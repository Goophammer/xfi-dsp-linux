// SPDX-License-Identifier: GPL-2.0
/*
 * ct_winrec.c - ALSA capture from Windows mode's recording path: PCM device 7 "XFi Windows mic".
 *
 * Why: in Windows mode the card is wired the Windows way and
 * the Linux capture path is frozen, so the X-Fi could not record. Creative's driver records like this: the analog
 * input rings -> mixer -> converters 75/76 (rate conversion) -> mixer -> a memory-writing converter (WR=1, S32) that
 * loops a buffer in host memory. The firmware (fw_blob "capture" section) replays Windows' recording start made while
 * it played (trace xfi_20261030_122052: converter ct_winmode_cap_src() = 75 into device addresses WIN_REC_VA..,
 * ct_dsp_fx.c), once, at load; the converter then records continuously.
 *
 * This device is ct_winplay's mirror image: a soft hrtimer reads the converter's current write address (CA) and
 * copies every frame written since the previous tick from the loop into the ALSA buffer. The loop's frame layout is
 * winrec_loop_ch S32 channels: 2 (measured 2026-10-30: 96000 samples/s; the mic arrives as identical left/right);
 * winrec_left / winrec_right pick the two that become the stereo stream.
 */
#include <linux/hrtimer.h>
#include <linux/ktime.h>
#include <linux/module.h>
#include <linux/slab.h>
#include <sound/core.h>
#include <sound/pcm.h>
#include "ctatc.h"
#include "cthw20k1.h"
#include "ct_dsp_fx.h"
#include "ct_winmode.h"
#include "ct_winrec.h"

static unsigned int winrec_period_us = 2000;
module_param(winrec_period_us, uint, 0644);
MODULE_PARM_DESC(winrec_period_us, "Windows-path capture: copy interval in microseconds");
static unsigned int winrec_loop_ch = 2;
module_param(winrec_loop_ch, uint, 0644);
MODULE_PARM_DESC(winrec_loop_ch, "Windows-path capture: S32 channels per frame in the converter's loop");
static unsigned int winrec_left;
module_param(winrec_left, uint, 0644);
MODULE_PARM_DESC(winrec_left, "Windows-path capture: loop channel used as left");
static unsigned int winrec_right = 1;
module_param(winrec_right, uint, 0644);
MODULE_PARM_DESC(winrec_right, "Windows-path capture: loop channel used as right");

/*
 * The converter's position runs ahead of what has reached memory: copying up to it took a few stale frames (one loop old) on every
 * tick, a 500 Hz comb of artifacts at -38 dB, inaudible in tones that repeat with the loop (2026-11-05, "old tape" sound).
 */
static unsigned int winrec_behind = 64;
module_param(winrec_behind, uint, 0644);
MODULE_PARM_DESC(winrec_behind, "Windows-path capture: frames to stay behind the converter's position");

#define WINREC_PCM_DEVICE	7
#define SRC_BASE(n)		(0x1B0000 + (n) * 0x100)
#define ADDR_MASK		0x03FFFFFF

struct winrec {
	struct ct_atc *atc;
	struct snd_pcm_substream *ss;
	struct hrtimer timer;
	spinlock_t lock;
	bool running;
	u32 *loop;			/* CPU address of the loop (device address va) */
	u32 va, bytes;			/* loop: device address, length */
	u32 nch, nloop;			/* channels per loop frame, loop length in frames */
	u32 c_last;			/* converter write position (frames within the loop) at the previous tick */
	bool sync;			/* take the next valid position as c_last (start, or after a bad read) */
	snd_pcm_uframes_t hw_ptr, period_acc;
	u64 frames, ticks, bad;
	u64 silent;			/* frames of silence delivered since t0 (no recording path) */
	ktime_t t0;
};

static u32 rd(struct winrec *r, u32 reg)
{
	return ct20k1_reg_read(r->atc->hw, reg);
}

/* safe read position (winrec_behind frames behind the converter) in frames within the loop, -1 outside it, -2 when the card stopped answering */
static int cap_pos(struct winrec *r)
{
	u32 ca = rd(r, SRC_BASE(ct_winmode_cap_src()) + 0x10);

	if (ca == 0xFFFFFFFF) {
		ct_winmode_card_gone();
		return -2;
	}
	ca &= ADDR_MASK;
	if (ca < 0x1000 || ca == r->va + r->bytes)	/* at the wrap: a bare offset, or just past the end. Both are the start */
		ca = r->va;
	if (ca < r->va || ca >= r->va + r->bytes)
		return -1;
	return ((ca - r->va) / (4 * r->nch) + r->nloop - min(winrec_behind, r->nloop / 4)) % r->nloop;
}

static const struct snd_pcm_hardware winrec_hw = {
	.info		= SNDRV_PCM_INFO_MMAP | SNDRV_PCM_INFO_MMAP_VALID | SNDRV_PCM_INFO_BATCH |
			  SNDRV_PCM_INFO_INTERLEAVED | SNDRV_PCM_INFO_BLOCK_TRANSFER,
	.formats	= SNDRV_PCM_FMTBIT_S32_LE,
	.rates		= SNDRV_PCM_RATE_48000,
	.rate_min	= 48000,
	.rate_max	= 48000,
	.channels_min	= 2,
	.channels_max	= 2,
	.buffer_bytes_max = 512 * 1024,
	.period_bytes_min = 256,
	.period_bytes_max = 128 * 1024,
	.periods_min	= 2,
	.periods_max	= 1024,
};

static enum hrtimer_restart winrec_tick(struct hrtimer *t)
{
	struct winrec *r = container_of(t, struct winrec, timer);
	struct snd_pcm_runtime *rt;
	bool elapsed = false;
	unsigned long flags;
	u32 n, i, l, rr, *dst;
	int c = -1;

	spin_lock_irqsave(&r->lock, flags);
	if (!r->running || !r->ss) {
		spin_unlock_irqrestore(&r->lock, flags);
		return HRTIMER_NORESTART;
	}
	rt = r->ss->runtime;
	r->ticks++;
	if (r->loop && ct_winmode_running()) {
		c = cap_pos(r);
		if (c == -1) {			/* a position outside the loop now and then: skip this tick */
			r->bad++;
			r->sync = true;
			goto out;
		}
	}
	if (c < 0) {
		/*
		 * No recording path (Windows mode not up, or the card is gone): silence in real time. Signed and capped:
		 * a negative count once became ~4 billion frames in this timer - a frozen PC for seconds (2026-10-30).
		 */
		s64 due = ktime_us_delta(ktime_get(), r->t0) * 48 / 1000 - (s64)r->silent;

		n = (u32)clamp_t(s64, due, 0, rt->buffer_size);
		r->silent += n;
		dst = (u32 *)rt->dma_area;
		i = 0;
		goto silence;
	}
	if (r->sync) {
		r->sync = false;
		r->c_last = c;
		goto out;
	}
	n = ((u32)c + r->nloop - r->c_last) % r->nloop;
	if (n >= r->nloop / 2) {		/* lost track (timer very late): resynchronise */
		r->c_last = c;
		goto out;
	}
	dst = (u32 *)rt->dma_area;
	l = min(winrec_left, r->nch - 1);
	rr = min(winrec_right, r->nch - 1);
	for (i = 0; i < n; i++) {
		u32 *f = r->loop + (size_t)r->c_last * r->nch;

		dst[2 * r->hw_ptr] = f[l];
		dst[2 * r->hw_ptr + 1] = f[rr];
		if (++r->c_last >= r->nloop)
			r->c_last = 0;
		if (++r->hw_ptr >= rt->buffer_size)
			r->hw_ptr = 0;
		if (++r->period_acc >= rt->period_size) {
			r->period_acc -= rt->period_size;
			elapsed = true;
		}
	}
	r->frames += n;
	goto out;
silence:
	for (; i < n; i++) {
		dst[2 * r->hw_ptr] = 0;
		dst[2 * r->hw_ptr + 1] = 0;
		if (++r->hw_ptr >= rt->buffer_size)
			r->hw_ptr = 0;
		if (++r->period_acc >= rt->period_size) {
			r->period_acc -= rt->period_size;
			elapsed = true;
		}
	}
	r->frames += n;
out:
	spin_unlock_irqrestore(&r->lock, flags);
	if (elapsed)
		snd_pcm_period_elapsed(r->ss);
	hrtimer_forward_now(t, ns_to_ktime((u64)(winrec_period_us ? winrec_period_us : 2000) * 1000));
	return HRTIMER_RESTART;
}

static int winrec_open(struct snd_pcm_substream *ss)
{
	struct winrec *r = snd_pcm_substream_chip(ss);

	ss->runtime->hw = winrec_hw;
	r->ss = ss;
	return 0;
}

static int winrec_close(struct snd_pcm_substream *ss)
{
	struct winrec *r = snd_pcm_substream_chip(ss);

	hrtimer_cancel(&r->timer);
	r->ss = NULL;
	return 0;
}

static int winrec_prepare(struct snd_pcm_substream *ss)
{
	struct winrec *r = snd_pcm_substream_chip(ss);
	size_t size;

	r->nch = clamp(winrec_loop_ch, 1u, 8u);
	r->loop = ct_winmode_cap_src() ? ct_dsp_fx_rec_buf(&r->va, &size) : NULL;
	r->bytes = size;
	r->nloop = r->loop ? r->bytes / (4 * r->nch) : 0;
	if (!r->nloop)
		r->loop = NULL;
	r->hw_ptr = 0;
	r->period_acc = 0;
	return 0;
}

static int winrec_trigger(struct snd_pcm_substream *ss, int cmd)
{
	struct winrec *r = snd_pcm_substream_chip(ss);
	struct device *dev = r->atc->card->dev;
	unsigned long flags;
	u64 us;
	int c;

	switch (cmd) {
	case SNDRV_PCM_TRIGGER_START:
		c = (r->loop && ct_winmode_running()) ? cap_pos(r) : -2;
		spin_lock_irqsave(&r->lock, flags);
		r->c_last = c < 0 ? 0 : c;
		r->sync = c < 0;		/* recording path but no valid position yet: sync on the first good one */
		r->frames = r->ticks = r->bad = r->silent = 0;
		r->t0 = ktime_get();
		r->running = true;
		spin_unlock_irqrestore(&r->lock, flags);
		hrtimer_start(&r->timer, ns_to_ktime((u64)winrec_period_us * 1000), HRTIMER_MODE_REL_SOFT);
		if (c != -2)
			dev_info(dev, "X-Fi Windows mic: recording from converter %u loop %#x (%u frames x %u ch)\n",
				 ct_winmode_cap_src(), r->va, r->nloop, r->nch);
		else
			dev_info(dev, "X-Fi Windows mic: no recording path (Windows mode not running or its firmware has none) - silence\n");
		return 0;
	case SNDRV_PCM_TRIGGER_STOP:
		spin_lock_irqsave(&r->lock, flags);
		r->running = false;
		spin_unlock_irqrestore(&r->lock, flags);
		hrtimer_try_to_cancel(&r->timer);
		us = ktime_us_delta(ktime_get(), r->t0);
		dev_info(dev, "X-Fi Windows mic: stopped after %llu ms: %llu frames (%llu frames/s), %llu bad positions\n",
			 us / 1000, r->frames, us ? r->frames * 1000000ULL / us : 0, r->bad);
		return 0;
	default:
		return -EINVAL;
	}
}

static snd_pcm_uframes_t winrec_pointer(struct snd_pcm_substream *ss)
{
	struct winrec *r = snd_pcm_substream_chip(ss);

	return READ_ONCE(r->hw_ptr);
}

static const struct snd_pcm_ops winrec_ops = {
	.open		= winrec_open,
	.close		= winrec_close,
	.prepare	= winrec_prepare,
	.trigger	= winrec_trigger,
	.pointer	= winrec_pointer,
};

static void winrec_pcm_free(struct snd_pcm *pcm)
{
	struct winrec *r = pcm->private_data;

	if (!r)
		return;
	hrtimer_cancel(&r->timer);
	kfree(r);
}

int ct_winrec_create(struct ct_atc *atc)
{
	struct snd_pcm *pcm;
	struct winrec *r;
	int err;

	if (atc->chip_type != ATC20K1)
		return 0;
	r = kzalloc(sizeof(*r), GFP_KERNEL);
	if (!r)
		return -ENOMEM;
	r->atc = atc;
	spin_lock_init(&r->lock);
	hrtimer_setup(&r->timer, winrec_tick, CLOCK_MONOTONIC, HRTIMER_MODE_REL_SOFT);
	err = snd_pcm_new(atc->card, "XFi Windows mic", WINREC_PCM_DEVICE, 0, 1, &pcm);
	if (err < 0) {
		kfree(r);
		return err;
	}
	pcm->private_data = r;
	pcm->private_free = winrec_pcm_free;
	strscpy(pcm->name, "XFi Windows mic (recording in Windows mode)", sizeof(pcm->name));
	snd_pcm_set_ops(pcm, SNDRV_PCM_STREAM_CAPTURE, &winrec_ops);
	snd_pcm_set_managed_buffer_all(pcm, SNDRV_DMA_TYPE_VMALLOC, NULL, 0, 0);
	return 0;
}
