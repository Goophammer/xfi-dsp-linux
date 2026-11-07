// SPDX-License-Identifier: GPL-2.0
/*
 * ct_winplay.c - ALSA playback into Windows mode's playback path: PCM device 6 "XFi Windows path".
 *
 * Once Windows mode (ct_winmode.c) has replayed Creative's driver start, the playback converter (ct_winmode_src())
 * loops a buffer in our memory (device addresses SA..LA, S32 stereo, 48 kHz; the converter reloads SA/LA from its
 * address list at every loop) and its output runs through the X-Fi's DSP and Windows' mixer to the DAC.
 *
 * This device feeds that loop. The converter keeps looping on its own; a soft hrtimer
 * (winplay_period_us) reads the converter's current read address (CA) and, for every frame the converter consumed
 * since the previous tick, copies one frame from the ALSA buffer into the loop, winplay_lead_frames ahead of CA.
 * The ALSA pointer advances with the converter, so ALSA's clock IS the converter's clock - no rate matching needed.
 * Loop memory = the driver's 'audio' transport buffer (ct_dsp_fx.c, device address WIN_PLAY_VA); SA/LA are read from
 * the converter at start. Prepare and stop fill the loop with silence. Without Windows mode the device still opens
 * and discards in real time, switching to the loop once Windows mode runs.
 *
 * Usage: sudo ./src/winmode_start.sh, then e.g.
 *   aplay -D hw:CARD=XFi,DEV=6 -f S32_LE -c 2 -r 48000 file.wav    (or any player via plughw:...,6, or PipeWire)
 */
#include <linux/hrtimer.h>
#include <linux/ktime.h>
#include <linux/module.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <sound/control.h>
#include <sound/core.h>
#include <sound/pcm.h>
#include <sound/tlv.h>
#include "ctatc.h"
#include "cthw20k1.h"
#include "ct_dsp_fx.h"
#include "ct_winplay.h"
#include "ct_winmode.h"

static unsigned int winplay_period_us = 2000;
module_param(winplay_period_us, uint, 0644);
MODULE_PARM_DESC(winplay_period_us, "Windows-path playback: copy interval in microseconds");

static unsigned int winplay_lead_frames = 480;
module_param(winplay_lead_frames, uint, 0644);
MODULE_PARM_DESC(winplay_lead_frames, "Windows-path playback: frames written ahead of the converter's read address");

#define WINPLAY_PCM_DEVICE	6

/*
 * "DSP Master Playback Volume", 0..100 = -50 dB .. 0 dB in 0.5 dB steps. With a firmware that has Windows' master
 * volume (ct_winmode: parameter gains AFTER the effects + the volume the effects are told, as Windows' volume slider
 * does) it drives that, and the samples go in unscaled - scaling them ahead of the DSP made SVM over-compensate
 * (2026-10-30). Without one it scales the samples while copying into the loop. PipeWire uses it as the sink's
 * hardware volume (src/xfi-dsp ACP profile).
 */
#define WINPLAY_VOL_MAX		100
static const u32 winplay_gain_q16[WINPLAY_VOL_MAX + 1] = {
207, 220, 233, 246, 261, 276, 293, 310, 328, 348,
369, 390, 414, 438, 464, 491, 521, 551, 584, 619,
655, 694, 735, 779, 825, 874, 926, 981, 1039, 1100,
1165, 1234, 1308, 1385, 1467, 1554, 1646, 1744, 1847, 1957,
2072, 2195, 2325, 2463, 2609, 2764, 2927, 3101, 3285, 3479,
3685, 3904, 4135, 4380, 4640, 4915, 5206, 5514, 5841, 6187,
6554, 6942, 7353, 7789, 8250, 8739, 9257, 9806, 10387, 11002,
11654, 12345, 13076, 13851, 14672, 15541, 16462, 17437, 18471, 19565,
20724, 21952, 23253, 24631, 26090, 27636, 29274, 31008, 32846, 34792,
36854, 39037, 41350, 43801, 46396, 49145, 52057, 55142, 58409, 61870,
65536
};
static const DECLARE_TLV_DB_SCALE(winplay_vol_tlv, -5000, 50, 1);
/* the playback converter comes from the loaded firmware (Game mode SRC 202, Entertainment mode SRC 30) */
#define SRC_BASE(n)		(0x1B0000 + (n) * 0x100)
#define SRC_CA(p)		((p)->src_base + 0x10)
#define SRC_SA(p)		((p)->src_base + 0x18)
#define SRC_LA(p)		((p)->src_base + 0x1C)
#define ADDR_MASK		0x03FFFFFF

struct winplay {
	struct ct_atc *atc;
	struct snd_pcm_substream *ss;
	struct hrtimer timer;
	spinlock_t lock;
	bool running;
	u32 src_base;			/* playback converter's registers */
	u32 *loop;			/* CPU address of device address sa */
	u32 sa, la, nloop;		/* converter loop (device addresses), length in frames */
	u32 ch;				/* channels of this stream; the loop needs the firmware's (2 or 8) */
	u32 c_last, w;			/* converter position / our write position, frames within the loop */
	snd_pcm_uframes_t hw_ptr;	/* frames consumed from the ALSA buffer */
	snd_pcm_uframes_t period_acc;
	u64 frames, ticks, resyncs, bad, late;
	ktime_t t0;
	u64 free_frames;		/* free-running (no Windows mode yet): frames discarded since t0 */
	unsigned int vol;		/* index into winplay_gain_q16 */
};

/*
 * Windows folds LFE into front left and right before the DSP, with CMSS-3D on or off: measured on What U Hear, +5.5 dB
 * against a front channel, flat with CMSS off and with the fronts' CMSS curve with it on. Left on its own channel it skips
 * CMSS and reaches the ears 8.5 dB lower. 1.884 in 16.16.
 */
#define WINPLAY_LFE_GAIN	123444

static u32 rd(struct winplay *p, u32 reg)
{
	return ct20k1_reg_read(p->atc->hw, reg);
}

/* converter position in frames within the loop, or -1 if CA is outside it */
static int conv_pos(struct winplay *p)
{
	u32 ca = rd(p, SRC_CA(p));

	if (ca == 0xFFFFFFFF) {		/* the card stopped answering */
		ct_winmode_card_gone();
		return -1;
	}
	ca &= ADDR_MASK;

	if (ca < 0x1000 || ca == p->la)	/* at the wrap, for some 40 us: a bare offset, or just past the end. Both are the start */
		return 0;
	if (ca < p->sa || ca >= p->la)
		return -1;
	return (ca - p->sa) / (4 * p->ch);
}

static void loop_silence(struct winplay *p)
{
	if (p->loop)
		memset(p->loop, 0, (size_t)p->nloop * 4 * p->ch);
}

static const struct snd_pcm_hardware winplay_hw = {
	/* BATCH: the position advances in winplay_period_us steps - lets PipeWire/PulseAudio plan their headroom */
	.info		= SNDRV_PCM_INFO_MMAP | SNDRV_PCM_INFO_MMAP_VALID | SNDRV_PCM_INFO_BATCH |
			  SNDRV_PCM_INFO_INTERLEAVED | SNDRV_PCM_INFO_BLOCK_TRANSFER,
	.formats	= SNDRV_PCM_FMTBIT_S32_LE,
	.rates		= SNDRV_PCM_RATE_48000,
	.rate_min	= 48000,
	.rate_max	= 48000,
	.channels_min	= 2,
	.channels_max	= 8,
	.buffer_bytes_max = 512 * 1024,
	.period_bytes_min = 256,
	.period_bytes_max = 128 * 1024,
	.periods_min	= 2,
	.periods_max	= 1024,
};

static int winplay_setup_loop_quiet(struct winplay *p, bool warn);

static enum hrtimer_restart winplay_tick(struct hrtimer *t)
{
	struct winplay *p = container_of(t, struct winplay, timer);
	struct snd_pcm_runtime *rt;
	bool elapsed = false;
	unsigned long flags;
	u32 n, i, g = 65536, *src = NULL;
	int c;

	spin_lock_irqsave(&p->lock, flags);
	if (!p->running || !p->ss) {
		spin_unlock_irqrestore(&p->lock, flags);
		return HRTIMER_NORESTART;
	}
	rt = p->ss->runtime;
	p->ticks++;
	if (!p->loop) {
		/*
		 * free-running; switch to the converter once Windows mode is up. Checked every 50 ticks only, and never
		 * once the card is gone: reads of a dead card can take milliseconds each (2026-10-30 freeze).
		 */
		if (!(p->ticks % 50) && ct_winmode_running() && !winplay_setup_loop_quiet(p, false) &&
		    (c = conv_pos(p)) >= 0) {
			p->c_last = c;
			p->w = (c + winplay_lead_frames) % p->nloop;
			goto out;
		}
		p->loop = NULL;
		n = (u32)clamp_t(s64, ktime_us_delta(ktime_get(), p->t0) * 48 / 1000 - (s64)p->free_frames,
				 0, rt->buffer_size);	/* signed + capped: see ct_winrec.c */
		p->free_frames += n;
		goto advance;
	}
	c = conv_pos(p);
	if (c < 0 && !ct_winmode_running()) {	/* card gone (or Windows mode stopped): free-run, no more reads */
		p->loop = NULL;
		p->t0 = ktime_get();
		p->free_frames = 0;
		goto out;
	}
	if (c < 0) {
		p->bad++;
		goto out;
	}
	n = ((u32)c + p->nloop - p->c_last) % p->nloop;
	if (n >= p->nloop / 2) {			/* lost track (timer very late / converter restarted) */
		p->resyncs++;
		p->c_last = c;
		p->w = (c + winplay_lead_frames) % p->nloop;
		goto out;
	}
	if (n > winplay_lead_frames)			/* the converter overtook our write position */
		p->late++;
	p->c_last = c;
	src = (u32 *)rt->dma_area;
	g = ct_winmode_has_master() ? 65536 : winplay_gain_q16[min(READ_ONCE(p->vol), (unsigned int)WINPLAY_VOL_MAX)];
advance:
	for (i = 0; i < n; i++) {
		if (p->loop) {
			u32 *d = p->loop + (size_t)p->w * p->ch;
			u32 *f = src + (size_t)p->hw_ptr * p->ch;

			for (u32 k = 0; k < p->ch; k++)
				d[k] = g == 65536 ? f[k] : (u32)(s32)(((s64)(s32)f[k] * g) >> 16);
			if (p->ch == 8) {	/* FL FR FC LFE ...: see WINPLAY_LFE_GAIN */
				s64 lfe = ((s64)(s32)d[3] * WINPLAY_LFE_GAIN) >> 16;

				d[0] = (u32)(s32)clamp_t(s64, (s64)(s32)d[0] + lfe, S32_MIN, S32_MAX);
				d[1] = (u32)(s32)clamp_t(s64, (s64)(s32)d[1] + lfe, S32_MIN, S32_MAX);
				d[3] = 0;
			}
		}
		if (p->loop && ++p->w >= p->nloop)
			p->w = 0;
		if (++p->hw_ptr >= rt->buffer_size)
			p->hw_ptr = 0;
		if (++p->period_acc >= rt->period_size) {
			p->period_acc -= rt->period_size;
			elapsed = true;
		}
	}
	p->frames += n;
out:
	spin_unlock_irqrestore(&p->lock, flags);
	if (elapsed)
		snd_pcm_period_elapsed(p->ss);
	hrtimer_forward_now(t, ns_to_ktime((u64)(winplay_period_us ? winplay_period_us : 2000) * 1000));
	return HRTIMER_RESTART;
}

static int winplay_open(struct snd_pcm_substream *ss)
{
	struct winplay *p = snd_pcm_substream_chip(ss);

	p->ch = ct_winmode_channels();
	ss->runtime->hw = winplay_hw;
	/*
	 * Before the firmware is loaded its channel count is unknown, and PipeWire probes this device at its start,
	 * usually before that: offer everything then, the firmware's count once it runs.
	 */
	if (ct_winmode_running()) {
		ss->runtime->hw.channels_min = p->ch;
		ss->runtime->hw.channels_max = p->ch;
	}
	p->ss = ss;
	return 0;
}

static int winplay_close(struct snd_pcm_substream *ss)
{
	struct winplay *p = snd_pcm_substream_chip(ss);

	hrtimer_cancel(&p->timer);
	p->ss = NULL;
	return 0;
}

/* SA/LA from the converter; the loop must lie inside our 'audio' buffer */
static int winplay_setup_loop_quiet(struct winplay *p, bool warn)
{
	struct device *dev = p->atc->card->dev;
	u32 va;
	size_t size;
	void *buf = ct_dsp_fx_audio_buf(&va, &size);

	if (p->ch != ct_winmode_channels()) {	/* opened before the firmware came up, with another channel count */
		p->loop = NULL;
		if (warn)
			dev_warn(dev, "X-Fi Windows path: stream has %u channels, the firmware %u - stream stays silent\n",
				 p->ch, ct_winmode_channels());
		return -EIO;
	}
	p->src_base = SRC_BASE(ct_winmode_src());
	p->sa = rd(p, SRC_SA(p));
	if (p->sa == 0xFFFFFFFF) {
		ct_winmode_card_gone();
		p->loop = NULL;
		return -EIO;
	}
	p->sa &= ADDR_MASK;
	p->la = rd(p, SRC_LA(p)) & ADDR_MASK;
	if (!buf || p->sa < va || p->la <= p->sa || p->la - va > size ||
	    (p->la - p->sa) % (4 * p->ch)) {
		p->loop = NULL;
		if (!warn)
			return -EIO;
		dev_warn(dev, "X-Fi Windows path: converter loop %#x..%#x is not inside the audio buffer (%#x, %zu bytes) - start Windows mode first (src/winmode_start.sh)\n",
			 p->sa, p->la, va, size);
		p->loop = NULL;
		return -EIO;
	}
	p->loop = (u32 *)((char *)buf + (p->sa - va));
	p->nloop = (p->la - p->sa) / (4 * p->ch);
	if (winplay_lead_frames >= p->nloop / 2)
		winplay_lead_frames = p->nloop / 4;
	return 0;
}

static int winplay_setup_loop(struct winplay *p)
{
	return winplay_setup_loop_quiet(p, true);
}

/*
 * Without Windows mode the device still works (PipeWire probes it and keeps its sink): the stream free-runs at
 * 48 kHz and its frames are discarded until Windows mode is running, then it switches to the converter loop.
 */
static int winplay_prepare(struct snd_pcm_substream *ss)
{
	struct winplay *p = snd_pcm_substream_chip(ss);

	p->ch = ss->runtime->channels;
	if (ct_winmode_running())
		winplay_setup_loop(p);
	else
		p->loop = NULL;
	loop_silence(p);
	p->hw_ptr = 0;
	p->period_acc = 0;
	return 0;
}

static int winplay_trigger(struct snd_pcm_substream *ss, int cmd)
{
	struct winplay *p = snd_pcm_substream_chip(ss);
	struct device *dev = p->atc->card->dev;
	unsigned long flags;
	u64 us;
	int c;

	switch (cmd) {
	case SNDRV_PCM_TRIGGER_START:
		c = p->loop ? conv_pos(p) : -1;
		if (p->loop && c < 0)
			dev_warn(dev, "X-Fi Windows path: the playback converter is not running inside its loop (CA %#x) - discarding until it is\n",
				 rd(p, SRC_CA(p)));
		spin_lock_irqsave(&p->lock, flags);
		if (c < 0) {
			p->loop = NULL;
		} else {
			p->c_last = c;
			p->w = (c + winplay_lead_frames) % p->nloop;
		}
		p->free_frames = 0;
		p->frames = p->ticks = p->resyncs = p->bad = p->late = 0;
		p->t0 = ktime_get();
		p->running = true;
		spin_unlock_irqrestore(&p->lock, flags);
		hrtimer_start(&p->timer, ns_to_ktime((u64)winplay_period_us * 1000), HRTIMER_MODE_REL_SOFT);
		if (p->loop)
			dev_info(dev, "X-Fi Windows path: playing into converter loop %#x..%#x (%u frames), lead %u frames, tick %u us\n",
				 p->sa, p->la, p->nloop, winplay_lead_frames, winplay_period_us);
		else
			dev_info(dev, "X-Fi Windows path: Windows mode not running - discarding until it is\n");
		return 0;
	case SNDRV_PCM_TRIGGER_STOP:
		spin_lock_irqsave(&p->lock, flags);
		p->running = false;
		spin_unlock_irqrestore(&p->lock, flags);
		hrtimer_try_to_cancel(&p->timer);
		loop_silence(p);
		us = ktime_us_delta(ktime_get(), p->t0);
		dev_info(dev, "X-Fi Windows path: stopped after %llu ms: %llu frames (%llu frames/s), %llu ticks, %llu late, %llu resyncs, %llu bad positions\n",
			 us / 1000, p->frames, us ? p->frames * 1000000ULL / us : 0, p->ticks, p->late, p->resyncs, p->bad);
		return 0;
	default:
		return -EINVAL;
	}
}

static snd_pcm_uframes_t winplay_pointer(struct snd_pcm_substream *ss)
{
	struct winplay *p = snd_pcm_substream_chip(ss);

	return READ_ONCE(p->hw_ptr);
}

static const struct snd_pcm_ops winplay_ops = {
	.open		= winplay_open,
	.close		= winplay_close,
	.prepare	= winplay_prepare,
	.trigger	= winplay_trigger,
	.pointer	= winplay_pointer,
};

static int winplay_vol_info(struct snd_kcontrol *kc, struct snd_ctl_elem_info *ui)
{
	ui->type = SNDRV_CTL_ELEM_TYPE_INTEGER;
	ui->count = 1;
	ui->value.integer.min = 0;
	ui->value.integer.max = WINPLAY_VOL_MAX;
	return 0;
}

static int winplay_vol_get(struct snd_kcontrol *kc, struct snd_ctl_elem_value *uv)
{
	struct winplay *p = snd_kcontrol_chip(kc);

	uv->value.integer.value[0] = p->vol;
	return 0;
}

static int winplay_vol_put(struct snd_kcontrol *kc, struct snd_ctl_elem_value *uv)
{
	struct winplay *p = snd_kcontrol_chip(kc);
	long v = uv->value.integer.value[0];

	if (v < 0 || v > WINPLAY_VOL_MAX)
		return -EINVAL;
	if (v == p->vol)
		return 0;
	WRITE_ONCE(p->vol, v);
	ct_winmode_set_master(v);
	return 1;
}

static const struct snd_kcontrol_new winplay_vol_ctl = {
	.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
	.name = WM_MASTER_NAME,
	.access = SNDRV_CTL_ELEM_ACCESS_READWRITE | SNDRV_CTL_ELEM_ACCESS_TLV_READ,
	.info = winplay_vol_info,
	.get = winplay_vol_get,
	.put = winplay_vol_put,
	.tlv = { .p = winplay_vol_tlv },
};

static struct winplay *winplay_dev;	/* for ct_winmode (one 20K1 card) */

static void winplay_pcm_free(struct snd_pcm *pcm)
{
	struct winplay *p = pcm->private_data;

	if (!p)
		return;
	hrtimer_cancel(&p->timer);
	if (winplay_dev == p)
		winplay_dev = NULL;
	kfree(p);
}

unsigned int ct_winplay_master(void)
{
	return winplay_dev ? READ_ONCE(winplay_dev->vol) : WINPLAY_VOL_MAX;
}

int ct_winplay_create(struct ct_atc *atc)
{
	struct snd_pcm *pcm;
	struct winplay *p;
	int err;

	if (atc->chip_type != ATC20K1)
		return 0;
	p = kzalloc(sizeof(*p), GFP_KERNEL);
	if (!p)
		return -ENOMEM;
	p->atc = atc;
	spin_lock_init(&p->lock);
	hrtimer_setup(&p->timer, winplay_tick, CLOCK_MONOTONIC, HRTIMER_MODE_REL_SOFT);
	err = snd_pcm_new(atc->card, "XFi Windows path", WINPLAY_PCM_DEVICE, 1, 0, &pcm);
	if (err < 0) {
		kfree(p);
		return err;
	}
	pcm->private_data = p;
	pcm->private_free = winplay_pcm_free;
	strscpy(pcm->name, "XFi Windows path (via the X-Fi DSP)", sizeof(pcm->name));
	snd_pcm_set_ops(pcm, SNDRV_PCM_STREAM_PLAYBACK, &winplay_ops);
	snd_pcm_set_managed_buffer_all(pcm, SNDRV_DMA_TYPE_VMALLOC, NULL, 0, 0);
	p->vol = WINPLAY_VOL_MAX;			/* 0 dB: the Crystalizer was clean at full scale (2026-10-29) */
	winplay_dev = p;
	err = snd_ctl_add(atc->card, snd_ctl_new1(&winplay_vol_ctl, p));
	if (err)
		dev_warn(atc->card->dev, "X-Fi Windows path: volume control not created (%d)\n", err);
	return 0;
}
