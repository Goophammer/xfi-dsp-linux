// SPDX-License-Identifier: GPL-2.0
/*
 * Creative X-Fi 20K1 DSP Effects
 *
 * Loads Crystalizer/CMSS-3D/Reverb microcode into the 20K1 DSP cores.
 * Microcode extracted from CTEXFIFX.sys (Creative Windows driver).
 *
 * DSP memory map (ct20k1reg.h):
 *   DSPMICRO_START = 0x080000 (~200KB microcode region)
 *   DSP0IO_START   = 0x100000 (DSP0 I/O — Crystalizer)
 *   DSP1IO_START   = 0x102000 (DSP1 I/O — CMSS-3D)
 *   DSP2IO_START   = 0x104000 (DSP2 I/O — Reverb)
 *
 * Hardware access: indirect port I/O via io_base
 *   outl(reg,  io_base + 0x0)  <- address
 *   outl(data, io_base + 0x4)  <- data
 */

#include <linux/firmware.h>
#include <linux/moduleparam.h>
#include <linux/slab.h>
#include <linux/io.h>
#include <linux/pci.h>
#include <linux/unaligned.h>
#include <linux/mutex.h>
#include <linux/dma-mapping.h>
#include <sound/core.h>
#include <sound/control.h>
#include "ctatc.h"
#include "cthardware.h"
#include "cthw20k1.h"
#include "ctsrc.h"
#include "ctamixer.h"
#include "ctmixer.h"
#include "ct_dsp_fx.h"
#include "ct_winplay.h"
#include "ct_winrec.h"
#include "ct_winmode.h"

/* Firmware filenames — install to /lib/firmware/ */
#define FW_CRYSTALIZER	"creative/ct20k1_crystalizer.fw"
#define FW_CMSS3D	"creative/ct20k1_cmss3d.fw"
#define FW_REVERB	"creative/ct20k1_reverb.fw"

/* DSP load addresses */
#define DSPMICRO_START	0x080000U
#define DSPMICRO_DSP1	0x090000U
#define DSPMICRO_DSP2	0x0A0000U

/* Effect enable/parameter registers (from Windows driver RE) */
#define CRYST_ENABLE_REG	0x100D00U
#define CRYST_INTENSITY_REG	0x100D04U
#define CMSS3D_ENABLE_REG	0x102D00U
#define CMSS3D_MODE_REG		0x102D04U
#define REVERB_ENABLE_REG	0x104D00U
#define REVERB_SIZE_REG		0x104D04U

#define DSP_OUTPUT_RING_BASE	0x100U
#define DSP_OUTPUT_RING_STRIDE	0x10U
#define DSP_FX_OUTPUT_CHANNELS	2

/*
 * Ring slots the route's SRCs read (lane 0).  Defaults are DSP output slots 0/1.  Writable at runtime for testing
 * the route with known-good sources, e.g. playback SRC7/SRC6 lane 0 (0x071/0x061) for an audible L/R swap;
 * takes effect when the Crystalizer Switch is next turned on.
 */
static unsigned int dsp_route_ring[DSP_FX_OUTPUT_CHANNELS] = {
	DSP_OUTPUT_RING_BASE,
	DSP_OUTPUT_RING_BASE + DSP_OUTPUT_RING_STRIDE,
};
module_param_named(dsp_route_ring0, dsp_route_ring[0], uint, 0644);
MODULE_PARM_DESC(dsp_route_ring0, "Ring slot read by the DSP route's left SRC (default 0x100)");
module_param_named(dsp_route_ring1, dsp_route_ring[1], uint, 0644);
MODULE_PARM_DESC(dsp_route_ring1, "Ring slot read by the DSP route's right SRC (default 0x110)");

/* Distance between a source's conjugate lanes on the ring; 0 = 0x1000 / msr (lane 1 of 0x071 is 0x871 at msr 2). */
static unsigned int dsp_route_lane_stride;
module_param(dsp_route_lane_stride, uint, 0644);
MODULE_PARM_DESC(dsp_route_lane_stride, "Ring distance between conjugate lanes of the route input (0 = 0x1000/msr)");

/*
 * Sample-rate multiple for the route SRC itself. The route SRC reads the DSP's .output ring, which runs at the
 * captured DSP rate (Windows ran its DSP-output SRCs at msr 4 / RSR 2), NOT necessarily the Linux card's atc->msr.
 * A mismatch leaves the SRC reading the ring but not resampling (SRCCCR frozen, no audio). 0 = use atc->msr;
 * otherwise force this msr (try 4 to match a 192k DSP image).
 */
static unsigned int dsp_route_msr;
module_param(dsp_route_msr, uint, 0644);
MODULE_PARM_DESC(dsp_route_msr, "Rate multiple for the route SRC (0 = card atc->msr; try 4 to match the DSP rate)");

/*
 * dsp_route_direct=1: the route AMIXER reads the DSP .output ring slot DIRECTLY (no SRC). An SRC cannot read a DSP
 * .output ARC (CWA never advances); an AMIXER mixes a ring into the PCM SUM with no resampling. Use when the DSP
 * output is already at the card rate.
 */
static unsigned int dsp_route_direct;
module_param(dsp_route_direct, uint, 0644);
MODULE_PARM_DESC(dsp_route_direct, "1 = route AMIXER reads the DSP .output ring directly (no SRC)");

struct dsp_fx_output {
	struct src *src;
	struct srcimp *srcimp;
	struct amixer *amixer;
};

struct ct_dsp_fx_priv {
	/* loaded firmware */
	const struct firmware *fw_cryst;
	const struct firmware *fw_cmss3d;
	const struct firmware *fw_reverb;

	/* current state */
	bool enabled[CT_NUM_EFFECTS];
	int  cryst_intensity;	/* 0-100 */
	int  cmss3d_mode;	/* ct_cmss3d_mode */
	int  reverb_size;	/* 0-100 */
	bool loaded;
	struct dsp_fx_output output[DSP_FX_OUTPUT_CHANNELS];
	bool output_routed;
};

/*
 * A tiny synthetic input resource lets SRCIMP map a ring slot.  At msr > 1 SRCIMP adds one SRCIMAP entry per
 * conjugate lane, like the driver's working ring-input SRCs; lane n of the source sits n * stride further on.
 */
static void dsp_fx_input_master(struct rsc *rsc)
{
	rsc->conj = 0;
}

static void dsp_fx_input_next_conj(struct rsc *rsc)
{
	rsc->conj++;
}

static int dsp_fx_input_output_slot(const struct rsc *rsc)
{
	unsigned int stride = dsp_route_lane_stride ? dsp_route_lane_stride :
			      0x1000U / (rsc->msr ? rsc->msr : 1);

	return (rsc->idx + rsc->conj * stride) & 0xFFF;
}

static const struct rsc_ops dsp_fx_input_rsc_ops = {
	.master = dsp_fx_input_master,
	.next_conj = dsp_fx_input_next_conj,
	.output_slot = dsp_fx_input_output_slot,
};

/*
 * Debug: capture the effected DSP output. When nonzero, atc_pcm_capture_prepare reads this DSP .output ARC
 * (0x120 = slot 2) via the ring rsc below, instead of the mixer front mix, so `arecord` records the effected audio.
 * The capture path's bus-master recording SRC provides the conversion clock the dsp_route SRC lacked (CWA frozen).
 */
static unsigned int dsp_fx_capture_arc;
module_param(dsp_fx_capture_arc, uint, 0644);
MODULE_PARM_DESC(dsp_fx_capture_arc, "If set, capture reads this DSP .output ARC (0x120=slot2) - records effected audio");

static struct rsc dsp_fx_cap_rsc[2];
int ct_dsp_fx_capture_override(struct ct_atc *atc, struct rsc **p0, struct rsc **p1)
{
	if (!dsp_fx_capture_arc)
		return 0;
	memset(dsp_fx_cap_rsc, 0, sizeof(dsp_fx_cap_rsc));
	dsp_fx_cap_rsc[0].idx = dsp_fx_capture_arc & 0xFFF;
	dsp_fx_cap_rsc[0].msr = atc->msr;
	dsp_fx_cap_rsc[0].ops = &dsp_fx_input_rsc_ops;
	dsp_fx_cap_rsc[1].idx = dsp_fx_capture_arc & 0xFFF;
	dsp_fx_cap_rsc[1].msr = atc->msr;
	dsp_fx_cap_rsc[1].ops = &dsp_fx_input_rsc_ops;
	*p0 = &dsp_fx_cap_rsc[0];
	*p1 = &dsp_fx_cap_rsc[1];
	return 1;
}

static void dsp_fx_unroute_outputs(struct ct_atc *atc,
				   struct ct_dsp_fx_priv *priv)
{
	struct src_mgr *src_mgr = atc->rsc_mgrs[SRC];
	struct srcimp_mgr *srcimp_mgr = atc->rsc_mgrs[SRCIMP];
	struct amixer_mgr *amixer_mgr = atc->rsc_mgrs[AMIXER];
	int i;

	for (i = 0; i < DSP_FX_OUTPUT_CHANNELS; i++) {
		struct dsp_fx_output *out = &priv->output[i];

		if (out->amixer)
			out->amixer->ops->setup(out->amixer, NULL, 0, NULL);
		if (out->src) {
			out->src->ops->set_state(out->src, SRC_STATE_OFF);
			out->src->ops->commit_write(out->src);
		}
		if (out->srcimp) {
			out->srcimp->ops->unmap(out->srcimp);
			srcimp_mgr->put_srcimp(srcimp_mgr, out->srcimp);
			out->srcimp = NULL;
		}
		if (out->amixer) {
			amixer_mgr->put_amixer(amixer_mgr, out->amixer);
			out->amixer = NULL;
		}
		if (out->src) {
			src_mgr->put_src(src_mgr, out->src);
			out->src = NULL;
		}
	}
	priv->output_routed = false;
}

static int dsp_fx_route_outputs(struct ct_atc *atc,
				struct ct_dsp_fx_priv *priv)
{
	struct src_mgr *src_mgr = atc->rsc_mgrs[SRC];
	struct srcimp_mgr *srcimp_mgr = atc->rsc_mgrs[SRCIMP];
	struct amixer_mgr *amixer_mgr = atc->rsc_mgrs[AMIXER];
	int i, err;

	if (atc->chip_type != ATC20K1)
		return -ENODEV;
	if (!atc->pcm || !atc->pcm[0] || !atc->pcm[1] ||
	    !src_mgr || !srcimp_mgr || !amixer_mgr)
		return -ENODEV;
	if (priv->output_routed)
		return 0;

	for (i = 0; i < DSP_FX_OUTPUT_CHANNELS; i++) {
		struct dsp_fx_output *out = &priv->output[i];
		/*
		 * Run at the card's rate multiple like every working ring-input SRC (S/PDIF, line, mic): the SRC gets
		 * its conjugate at +0x80 (RSR = log2 msr), SRCIMP one entry per lane, and the AMIXER fills both lanes
		 * of the SUM pair.  At msr=1 the route SRC never delivered audio.
		 */
		unsigned int route_msr = dsp_route_msr ? dsp_route_msr : atc->msr;
		struct src_desc src_desc = {
			.multi = 1,
			.msr = route_msr,
			.mode = ARCRW,
		};
		struct srcimp_desc srcimp_desc = { .msr = route_msr };
		struct amixer_desc amixer_desc = { .msr = route_msr };
		struct rsc ring_input = {
			.idx = dsp_route_ring[i] & 0xFFF,
			.msr = route_msr,
			.ops = &dsp_fx_input_rsc_ops,
		};

		err = src_mgr->get_src(src_mgr, &src_desc, &out->src);
		if (err)
			goto error;

		err = srcimp_mgr->get_srcimp(srcimp_mgr, &srcimp_desc,
					     &out->srcimp);
		if (err)
			goto error;

		err = amixer_mgr->get_amixer(amixer_mgr, &amixer_desc,
					     &out->amixer);
		if (err)
			goto error;

		/* SRCIMP's input ARC is the DSP output ring slot. */
		err = out->srcimp->ops->map(out->srcimp, out->src, &ring_input);
		if (err)
			goto error;

		/* Add the converted DSP channel to the front-left/right PCM SUM. */
		out->amixer->ops->setup(out->amixer, &out->src->rsc,
					INIT_VOL, atc->pcm[i]);

		/*
		 * dsp_route_direct=1: point the AMIXER's X input DIRECTLY at the DSP .output ring slot, bypassing the SRC
		 * entirely. An SRC cannot read a DSP .output ARC (proven: CWA never advanced in any config), but an AMIXER
		 * just mixes a ring slot into its SUM - no resampling, clocked by the PCM SUM/DAC that already works (the
		 * dry playback proves that path). If the DSP output is already at the card rate, no SRC is needed. This
		 * mixes the effected slot straight into the front PCM SUM -> DAC.
		 */
		if (dsp_route_direct) {
			struct rsc direct_in = {
				.idx = dsp_route_ring[i] & 0xFFF,
				.msr = atc->msr,
				.ops = &dsp_fx_input_rsc_ops,
			};
			out->amixer->ops->setup(out->amixer, &direct_in,
						INIT_VOL, atc->pcm[i]);
		}

		/*
		 * Select an interpolation ROM (ROM=1), matching the Windows DSP-output SRCs (SRC24/25, SRCCTL ROM=1).
		 * The ARCRW default config leaves ROM=0, which reads the ring but performs no resampling - the SRC's
		 * SRCCCR stays frozen (observed 0x100) and no samples reach the DAC. With ROM selected the SRC actually
		 * converts the DSP .output ring to the card rate. (set_rom is the only convert-mode bit exposed through
		 * src_rsc_ops; WR is handled by the ARCRW default.)
		 */
		out->src->ops->set_rom(out->src, 1);

		/*
		 * Make the first (master) route SRC the phase master, matching Windows' working DSP-output master SRC24
		 * (SRCCTL 0xD25: PM=1, WR=0, ROM=1); its conjugate SRC25 (0x925) is PM=0. The ARCRW default leaves PM=0 on
		 * both, so neither is a phase master and the conversion never advances (CWA frozen). Set PM=1 on the master
		 * only; WR stays 0 (Windows' master has WR=0 - an earlier WR=1 attempt was a mis-decode).
		 */
		if (i == 0)
			out->src->ops->set_pm(out->src, 1);

		/*
		 * Put the ARCRW (ring-input) SRC into RUN and arm it for the
		 * synchronous start strobe.  get_src() already set this SRC's
		 * per-channel enable bit in SRCENB, but a ring-input SRC only
		 * begins converting once SRCENBS is pulsed by the manager-level
		 * commit below.  This mirrors atc_pcm_capture_start(), which
		 * starts its converting (ring) SRCs with exactly this sequence;
		 * the PCM playback path drives its SRCs via bus-master/DMA
		 * instead and so does not need it.  Omitting it left the SRC
		 * configured and reading the ring, but never actually running,
		 * so no signal reached the DAC.
		 */
		out->src->ops->set_state(out->src, SRC_STATE_RUN);
		out->src->ops->commit_write(out->src);
		src_mgr->src_enable_s(src_mgr, out->src);
	}

	/* Synchronously enable the bridge SRCs (pulses SRCENBS). */
	src_mgr->commit_write(src_mgr);

	dev_info(atc->card->dev, "X-Fi: DSP route on: rings 0x%03x/0x%03x, msr %u, SRC %d/%d\n",
		 dsp_route_ring[0] & 0xFFF, dsp_route_ring[1] & 0xFFF, atc->msr,
		 priv->output[0].src->rsc.ops->index(&priv->output[0].src->rsc),
		 priv->output[1].src->rsc.ops->index(&priv->output[1].src->rsc));
	priv->output_routed = true;
	return 0;

error:
	dsp_fx_unroute_outputs(atc, priv);
	return err;
}

/* ------------------------------------------------------------------
 * Low-level hardware access through cthw20k1.c's locked accessors.
 * The index/data port pair (io_base+0/+4) is shared with the driver's
 * interrupt and PCM code; unlocked access races with it.
 * ------------------------------------------------------------------ */
static inline void dsp_reg_write(struct hw *hw, u32 reg, u32 val)
{
	ct20k1_reg_write(hw, reg, val);	/* takes reg_20k1_lock */
}

static inline u32 dsp_reg_read(struct hw *hw, u32 reg)
{
	return ct20k1_reg_read(hw, reg);	/* takes reg_20k1_lock */
}

/* ------------------------------------------------------------------
 * Load a firmware blob word-by-word into DSP memory
 * ------------------------------------------------------------------ */
static int dsp_load_blob(struct hw *hw, u32 base,
			 const u8 *data, size_t size)
{
	size_t i;
	u32 word;

	if (!data || size == 0)
		return -EINVAL;

	/* Align down to 32-bit boundary */
	size &= ~3UL;

	for (i = 0; i < size; i += 4) {
		word = get_unaligned_le32(data + i);
		dsp_reg_write(hw, base + i, word);
	}

	return 0;
}

/* ------------------------------------------------------------------
 * Load all effect firmware files
 * Non-fatal: missing firmware just disables that effect
 * ------------------------------------------------------------------ */
static void ct_dsp_load_effects(struct ct_atc *atc,
				struct ct_dsp_fx_priv *priv)
{
	struct hw *hw = atc->hw;
	struct device *dev = atc->card->dev;
	int err;

	/* Crystalizer → DSP0 */
	err = firmware_request_nowarn(&priv->fw_cryst, FW_CRYSTALIZER, dev);
	if (err) {
		dev_dbg(dev, "X-Fi: Crystalizer fw not found, effect disabled\n");
	} else {
		err = dsp_load_blob(hw, DSPMICRO_START,
				    priv->fw_cryst->data,
				    priv->fw_cryst->size);
		if (!err)
			dev_info(dev, "X-Fi: Crystalizer DSP loaded (%zu B)\n",
				 priv->fw_cryst->size);
	}

	/* CMSS-3D → DSP1 */
	err = firmware_request_nowarn(&priv->fw_cmss3d, FW_CMSS3D, dev);
	if (err) {
		dev_dbg(dev, "X-Fi: CMSS-3D fw not found, effect disabled\n");
	} else {
		err = dsp_load_blob(hw, DSPMICRO_DSP1,
				    priv->fw_cmss3d->data,
				    priv->fw_cmss3d->size);
		if (!err)
			dev_info(dev, "X-Fi: CMSS-3D DSP loaded (%zu B)\n",
				 priv->fw_cmss3d->size);
	}

	/* Reverb → DSP2 */
	err = firmware_request_nowarn(&priv->fw_reverb, FW_REVERB, dev);
	if (err) {
		dev_dbg(dev, "X-Fi: Reverb fw not found, effect disabled\n");
	} else {
		err = dsp_load_blob(hw, DSPMICRO_DSP2,
				    priv->fw_reverb->data,
				    priv->fw_reverb->size);
		if (!err)
			dev_info(dev, "X-Fi: Reverb DSP loaded (%zu B)\n",
				 priv->fw_reverb->size);
	}

	priv->loaded = true;
}

/* ------------------------------------------------------------------
 * Effect enable/parameter helpers (called from ALSA controls)
 * ------------------------------------------------------------------ */
static int fx_set_enable(struct ct_atc *atc,
			 enum ct_dsp_effect fx, bool on)
{
	struct ct_dsp_fx_priv *priv = atc->dsp_fx;
	int err = 0;

	if (!priv || !priv->loaded)
		return -ENODEV;

	switch (fx) {
	case CT_EFFECT_CRYSTALIZER:
	case CT_EFFECT_CMSS3D:
	case CT_EFFECT_REVERB:
		break;
	default:
		return -EINVAL;
	}

	/* The DSP enable registers are still unknown.  For the Crystalizer
	 * switch, stage the verified output bridge independently so the route
	 * can be exercised with a known bypass image.
	 */
	mutex_lock(&atc->atc_mutex);
	if (fx == CT_EFFECT_CRYSTALIZER && on && !priv->enabled[fx])
		err = dsp_fx_route_outputs(atc, priv);
	else if (fx == CT_EFFECT_CRYSTALIZER && !on && priv->enabled[fx])
		dsp_fx_unroute_outputs(atc, priv);
	if (!err)
		priv->enabled[fx] = on;
	mutex_unlock(&atc->atc_mutex);
	if (err)
		return err;
	return 0;
}

void ct_dsp_fx_unroute(struct ct_atc *atc)
{
	struct ct_dsp_fx_priv *priv = atc->dsp_fx;

	if (!priv)
		return;
	mutex_lock(&atc->atc_mutex);
	if (priv->output_routed)
		dsp_fx_unroute_outputs(atc, priv);
	mutex_unlock(&atc->atc_mutex);
}

int ct_dsp_fx_resume(struct ct_atc *atc)
{
	struct ct_dsp_fx_priv *priv = atc->dsp_fx;
	int err = 0;

	if (!priv)
		return 0;
	mutex_lock(&atc->atc_mutex);
	if (priv->enabled[CT_EFFECT_CRYSTALIZER])
		err = dsp_fx_route_outputs(atc, priv);
	mutex_unlock(&atc->atc_mutex);
	return err;
}

static int fx_set_cryst_intensity(struct ct_atc *atc, int val)
{
	struct ct_dsp_fx_priv *priv = atc->dsp_fx;

	if (!priv || !priv->loaded)
		return -ENODEV;
	val = clamp(val, 0, 100);
	dsp_reg_write(atc->hw, CRYST_INTENSITY_REG,
		      (u32)((val * 0x7FFFFFFFUL) / 100));
	priv->cryst_intensity = val;
	return 0;
}

static int fx_set_cmss3d_mode(struct ct_atc *atc, int mode)
{
	struct ct_dsp_fx_priv *priv = atc->dsp_fx;

	if (!priv || !priv->loaded)
		return -ENODEV;
	dsp_reg_write(atc->hw, CMSS3D_MODE_REG, (u32)mode);
	priv->cmss3d_mode = mode;
	return 0;
}

static int fx_set_reverb_size(struct ct_atc *atc, int val)
{
	struct ct_dsp_fx_priv *priv = atc->dsp_fx;

	if (!priv || !priv->loaded)
		return -ENODEV;
	val = clamp(val, 0, 100);
	dsp_reg_write(atc->hw, REVERB_SIZE_REG,
		      (u32)((val * 0x7FFFFFFFUL) / 100));
	priv->reverb_size = val;
	return 0;
}

/* ------------------------------------------------------------------
 * ALSA mixer controls
 * ------------------------------------------------------------------ */

/* Generic boolean info shared by all switches */
static int fx_sw_info(struct snd_kcontrol *k, struct snd_ctl_elem_info *i)
{
	i->type = SNDRV_CTL_ELEM_TYPE_BOOLEAN;
	i->count = 1;
	i->value.integer.min = 0;
	i->value.integer.max = 1;
	return 0;
}

/* Generic integer 0-100 info */
static int fx_int_info(struct snd_kcontrol *k, struct snd_ctl_elem_info *i)
{
	i->type = SNDRV_CTL_ELEM_TYPE_INTEGER;
	i->count = 1;
	i->value.integer.min = 0;
	i->value.integer.max = 100;
	return 0;
}

/* --- Crystalizer Switch --- */
static bool legacy_fx_controls;
module_param(legacy_fx_controls, bool, 0444);
MODULE_PARM_DESC(legacy_fx_controls, "show the old (non-working) Crystalizer/CMSS-3D/Reverb mixer controls");

static int cryst_sw_get(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
	struct ct_atc *atc = snd_kcontrol_chip(k);
	struct ct_dsp_fx_priv *p = atc->dsp_fx;
	v->value.integer.value[0] = p ? p->enabled[CT_EFFECT_CRYSTALIZER] : 0;
	return 0;
}
static int cryst_sw_put(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
	return fx_set_enable(snd_kcontrol_chip(k), CT_EFFECT_CRYSTALIZER,
			     v->value.integer.value[0]);
}
static const struct snd_kcontrol_new cryst_sw = {
	.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
	.name  = "Crystalizer Switch",
	.info  = fx_sw_info,
	.get   = cryst_sw_get,
	.put   = cryst_sw_put,
};

/* --- Crystalizer Intensity --- */
static int cryst_int_get(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
	struct ct_atc *atc = snd_kcontrol_chip(k);
	struct ct_dsp_fx_priv *p = atc->dsp_fx;
	v->value.integer.value[0] = p ? p->cryst_intensity : 0;
	return 0;
}
static int cryst_int_put(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
	return fx_set_cryst_intensity(snd_kcontrol_chip(k),
				      v->value.integer.value[0]);
}
static const struct snd_kcontrol_new cryst_intensity = {
	.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
	.name  = "Crystalizer Intensity",
	.info  = fx_int_info,
	.get   = cryst_int_get,
	.put   = cryst_int_put,
};

/* --- CMSS-3D Switch --- */
static int cmss_sw_get(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
	struct ct_atc *atc = snd_kcontrol_chip(k);
	struct ct_dsp_fx_priv *p = atc->dsp_fx;
	v->value.integer.value[0] = p ? p->enabled[CT_EFFECT_CMSS3D] : 0;
	return 0;
}
static int cmss_sw_put(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
	return fx_set_enable(snd_kcontrol_chip(k), CT_EFFECT_CMSS3D,
			     v->value.integer.value[0]);
}
static const struct snd_kcontrol_new cmss_sw = {
	.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
	.name  = "CMSS-3D Switch",
	.info  = fx_sw_info,
	.get   = cmss_sw_get,
	.put   = cmss_sw_put,
};

/* --- CMSS-3D Mode --- */
static const char * const cmss_modes[] = {
	"Headphone", "Virtual", "Surround"
};
static int cmss_mode_info(struct snd_kcontrol *k, struct snd_ctl_elem_info *i)
{
	return snd_ctl_enum_info(i, 1, ARRAY_SIZE(cmss_modes), cmss_modes);
}
static int cmss_mode_get(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
	struct ct_atc *atc = snd_kcontrol_chip(k);
	struct ct_dsp_fx_priv *p = atc->dsp_fx;
	v->value.enumerated.item[0] = p ? p->cmss3d_mode : 0;
	return 0;
}
static int cmss_mode_put(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
	return fx_set_cmss3d_mode(snd_kcontrol_chip(k),
				  v->value.enumerated.item[0]);
}
static const struct snd_kcontrol_new cmss_mode = {
	.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
	.name  = "CMSS-3D Mode",
	.info  = cmss_mode_info,
	.get   = cmss_mode_get,
	.put   = cmss_mode_put,
};

/* --- Reverb Switch --- */
static int reverb_sw_get(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
	struct ct_atc *atc = snd_kcontrol_chip(k);
	struct ct_dsp_fx_priv *p = atc->dsp_fx;
	v->value.integer.value[0] = p ? p->enabled[CT_EFFECT_REVERB] : 0;
	return 0;
}
static int reverb_sw_put(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
	return fx_set_enable(snd_kcontrol_chip(k), CT_EFFECT_REVERB,
			     v->value.integer.value[0]);
}
static const struct snd_kcontrol_new reverb_sw = {
	.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
	.name  = "Reverb Switch",
	.info  = fx_sw_info,
	.get   = reverb_sw_get,
	.put   = reverb_sw_put,
};

/* --- Reverb Room Size --- */
static int reverb_size_get(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
	struct ct_atc *atc = snd_kcontrol_chip(k);
	struct ct_dsp_fx_priv *p = atc->dsp_fx;
	v->value.integer.value[0] = p ? p->reverb_size : 0;
	return 0;
}
static int reverb_size_put(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
	return fx_set_reverb_size(snd_kcontrol_chip(k),
				  v->value.integer.value[0]);
}
static const struct snd_kcontrol_new reverb_size = {
	.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
	.name  = "Reverb Room Size",
	.info  = fx_int_info,
	.get   = reverb_size_get,
	.put   = reverb_size_put,
};

int ct_dsp_fx_create_controls(struct ct_atc *atc)
{
	struct snd_card *card = atc->card;
	int err;

	/* the earlier effect emulation through the Linux routing (never worked; superseded by Windows mode, whose real
	 * DSP controls come from ct_winmode.c's effects table) - only with legacy_fx_controls=1 */
	if (legacy_fx_controls) {
		err = snd_ctl_add(card, snd_ctl_new1(&cryst_sw,        atc)); if (err) return err;
		err = snd_ctl_add(card, snd_ctl_new1(&cryst_intensity, atc)); if (err) return err;
		err = snd_ctl_add(card, snd_ctl_new1(&cmss_sw,         atc)); if (err) return err;
		err = snd_ctl_add(card, snd_ctl_new1(&cmss_mode,       atc)); if (err) return err;
		err = snd_ctl_add(card, snd_ctl_new1(&reverb_sw,       atc)); if (err) return err;
		err = snd_ctl_add(card, snd_ctl_new1(&reverb_size,     atc)); if (err) return err;
	}

	err = ct_winmode_create_controls(atc);
	if (err)
		dev_warn(card->dev, "X-Fi: Windows-mode effect controls not created (%d)\n", err);
	err = ct_winplay_create(atc);
	if (err)
		dev_warn(card->dev, "X-Fi: Windows-path playback PCM not created (%d)\n", err);
	err = ct_winrec_create(atc);
	if (err)
		dev_warn(card->dev, "X-Fi: Windows-path capture PCM not created (%d)\n", err);

	return 0;
}

/* ------------------------------------------------------------------
 * Init / Free
 * ------------------------------------------------------------------ */

int ct_dsp_fx_init(struct ct_atc *atc)
{
	struct ct_dsp_fx_priv *priv;

	priv = kzalloc(sizeof(*priv), GFP_KERNEL);
	if (!priv)
		return -ENOMEM;

	priv->cryst_intensity = 50;
	priv->cmss3d_mode     = CT_CMSS3D_HEADPHONE;
	priv->reverb_size     = 30;

	atc->dsp_fx = priv;

	ct_dsp_load_effects(atc, priv);

	ct_dsp_fx_sysfs_init(atc);
	dev_info(atc->card->dev, "X-Fi: DSP effects subsystem initialized\n");
	return 0;
}

void ct_dsp_fx_free(struct ct_atc *atc)
{
	struct ct_dsp_fx_priv *priv = atc->dsp_fx;

	if (!priv)
		return;

	ct_dsp_fx_unroute(atc);

	release_firmware(priv->fw_cryst);
	release_firmware(priv->fw_cmss3d);
	release_firmware(priv->fw_reverb);
	ct_dsp_fx_sysfs_free();
	kfree(priv);
	atc->dsp_fx = NULL;
}

/* ------------------------------------------------------------------
 * Sysfs debug interface for register access
 * Usage:
 *   Read:  echo "r 0x1C6070" > /sys/kernel/xfi_dsp/reg_access
 *          cat /sys/kernel/xfi_dsp/reg_access
 *   Write: echo "w 0x1C6070 0x00000010" > /sys/kernel/xfi_dsp/reg_access
 * ------------------------------------------------------------------ */

/* Log every reg_access read/write to the kernel log (bulk loaders turn this off while they run). */
static bool reg_access_log = true;
module_param(reg_access_log, bool, 0644);
MODULE_PARM_DESC(reg_access_log, "Log each /sys/kernel/xfi_dsp/reg_access read/write (default on)");

static u32  sysfs_last_reg;
static u32  sysfs_last_val;
static struct ct_atc *sysfs_atc;
static struct kobject *xfi_kobj;
/* read-only probes of the memory BARs ("m1 0x<off>", "m3 0x<off>"), mapped on first use */
static void __iomem *sysfs_bar[6];

static ssize_t reg_access_show(struct kobject *kobj,
                                struct kobj_attribute *attr, char *buf)
{
        if (!sysfs_atc)
                return sysfs_emit(buf, "no device\n");
        return sysfs_emit(buf, "reg=0x%08X val=0x%08X\n",
                          sysfs_last_reg, sysfs_last_val);
}

static ssize_t reg_access_store(struct kobject *kobj,
                                 struct kobj_attribute *attr,
                                 const char *buf, size_t count)
{
        int bar;
        u32 reg, val;
        struct hw *hw;

        if (!sysfs_atc)
                return -ENODEV;

        if (sysfs_atc->chip_type != ATC20K1)
                return -ENODEV;	/* accessors are 20K1-only */
        hw = sysfs_atc->hw;

        if (sscanf(buf, " r 0x%x", &reg) == 1) {
                val = dsp_reg_read(hw, reg);
                sysfs_last_reg = reg;
                sysfs_last_val = val;
                if (reg_access_log)
                        pr_info("xfi_dsp: read  0x%08X = 0x%08X\n", reg, val);
        } else if (sscanf(buf, " w 0x%x 0x%x", &reg, &val) == 2) {
                dsp_reg_write(hw, reg, val);
                sysfs_last_reg = reg;
                sysfs_last_val = val;
                if (reg_access_log)
                        pr_info("xfi_dsp: write 0x%08X = 0x%08X\n", reg, val);
        } else if (sscanf(buf, " m%d 0x%x", &bar, &reg) == 2) {
                struct pci_dev *pci = sysfs_atc->pci;

                if (bar < 0 || bar > 5 ||
                    !(pci_resource_flags(pci, bar) & IORESOURCE_MEM) ||
                    reg & 3 || reg + 4 > pci_resource_len(pci, bar))
                        return -EINVAL;
                if (!sysfs_bar[bar])
                        sysfs_bar[bar] = pci_iomap(pci, bar, 0);
                if (!sysfs_bar[bar])
                        return -EIO;
                val = ioread32(sysfs_bar[bar] + reg);	/* read only */
                sysfs_last_reg = reg;
                sysfs_last_val = val;
                pr_info("xfi_dsp: mmio%d 0x%08X = 0x%08X\n", bar, reg, val);
        } else {
                return -EINVAL;
        }

        return count;
}

static struct kobj_attribute reg_access_attr =
        __ATTR(reg_access, 0664, reg_access_show, reg_access_store);

/*
 * Host DMA buffers for the transport engine, sized from the Windows driver's transport map (VM trace 2026-10-28,
 * driver start): map entries are [pages << 16 | 0x1000, physHi, physLo, ...]; the map points
 * 1300 entries at one default page (MAPPLA too), one entry at a 72-page buffer and one at a 32-page buffer.
 * Linux never programs the transport map, so it never allocated these; Windows mode (ct_winmode.c) writes the map
 * with their bus addresses in place of Windows' addresses.
 */
/*
 * Windows-mode memory (2026-10-28, VM trace): Windows registers 16 device page-table pages (PTPALX/PTPAHX + 15
 * TANSPPAGETABLEPHYADDR entries; 64-bit entries = physical page per 4 KB device page, like Linux's ctvmem, 512 per
 * page -> 32 MB of device addresses), and plays through SRC 202 from device addresses 0x1b7d000-0x1b7ee00
 * (S32 stereo, 960 frames). The transport map entries are [device addr, physHi, physLo]: 0x481000 -> buf72,
 * 0x201000 -> buf32, 0x101000 -> BAR3 window, rest -> the default page. Our page tables point every device page at
 * the default page except those; "audio" holds a seamless 1 kHz S32 stereo test tone (20 periods in 960 frames).
 */
enum { XB_DEFAULT, XB_BUF72, XB_BUF32, XB_AUDIO, XB_PT0, XB_NUM = XB_PT0 + 16 };
static size_t xfer_buf_size[XB_NUM];
static char xfer_buf_name[XB_NUM][12];
static void *xfer_buf_cpu[XB_NUM];
static dma_addr_t xfer_buf_dma[XB_NUM];
#define WIN_PLAY_VA	0x1b7d000
/* recording loop (ct_winrec.c): where Windows records while it plays (VM trace xfi_20261030_122052: converter 75,
 * 0x1b55000..0x1b56e00, map entry 74). NOT near the playback buffer: 0x1b70000-0x1b7ffff is DSP memory (a loop at
 * 0x1b7b000 put the mic into the DSP - distortion, 2026-10-30). Backed by the xfer_full_mem chunks. */
#define WIN_REC_VA	0x1b55000
#define WIN_REC_BYTES	0x1e00
/* converter loop SA..SA + win_loop_frames * 8. Default = Windows' own 960 frames (0x1e00 bytes, 2 device pages).
 * 2026-10-29: with 4800 frames (10 pages, 0x1b7d000-0x1b86fff) everything through the DSP path was distorted - even
 * the idle tone - while the mixer-only path stayed clean: the pages after Windows' playback buffer are apparently
 * used by something else on the DSP side, so our buffer must not grow into them. Any multiple of 48 frames keeps the
 * idle tone seamless. Published in transport_bufs ('loop'). */
static unsigned int win_loop_frames = 960;
module_param(win_loop_frames, uint, 0444);
MODULE_PARM_DESC(win_loop_frames, "Windows mode: converter loop length in frames (960 = Windows' own buffer)");
#define WIN_LOOP_BYTES	(win_loop_frames * 8)
/* 8 pages: a 7.1 firmware's loop is 960 frames x 8 channels x 4 bytes = 0x7800 (0x1b7d000-0x1b84800, as Windows) */
#define WIN_AUDIO_BYTES	((size_t)max(8u, DIV_ROUND_UP(WIN_LOOP_BYTES, 4096u)) * 4096)
static bool winmode_test_tone;
module_param(winmode_test_tone, bool, 0444);
MODULE_PARM_DESC(winmode_test_tone, "Windows mode: idle 1 kHz test tone in the playback loop instead of silence");
static const s32 tone_1k[48] = {
	0, 70075716, 138952417, 205451603, 268435456, 326826303, 379625062, 425928331, 464943848, 496004047,
	518577479, 532277907, 536870912, 532277907, 518577479, 496004047, 464943848, 425928331, 379625062,
	326826303, 268435456, 205451603, 138952417, 70075716, 0, -70075716, -138952417, -205451603, -268435456,
	-326826303, -379625062, -425928331, -464943848, -496004047, -518577479, -532277907, -536870912,
	-532277907, -518577479, -496004047, -464943848, -425928331, -379625062, -326826303, -268435456,
	-205451603, -138952417, -70075716 };

/*
 * xfer_full_mem=1 (default): back EVERY device page (16 page tables x 512 x 4 KB = 32 MB) with its own zeroed memory
 * instead of aliasing unknown pages onto the 4 KB 'default' page. 2026-10-29: with Windows' DSP output path live, the
 * front chains carried static - the tank (DSP delay-line memory; native TKSCADR ~0x1EFCA0 while playing) reaches host
 * memory through these page tables, and all its delay lines collapsed onto one page. 32 chunks of 1 MB.
 */
static bool xfer_full_mem = true;
module_param(xfer_full_mem, bool, 0444);
MODULE_PARM_DESC(xfer_full_mem, "back all 32 MB of device pages with real memory (Windows mode tank)");
/*
 * tank_bar3=1 (default): device pages 0x1000-0x1FFFFF -> the card's OWN memory (BAR3), as Creative's Windows driver
 * maps them: device address D -> BAR3 + D - 0x1000 (real-Windows register dump 2026-10-30: transport map entries of
 * the DSP's delay-line streams 0x72000..0x1fe000 point to 0xE80xxxxx-0xE81xxxxx = BAR3; 0x101000 -> BAR3 + 1 MB is
 * also the trace's map entry). That 1 MB+ is the tank (delay-line memory). In host RAM it is reached over the PCIe-
 * to-PCI bridge: fine for Game mode (2x), but Entertainment mode (4x, more delay lines) starved - TKSC entries empty,
 * transport queue full (TRNQCNT 16 vs 3), tank entries flagged, music distorted. (In the VM the guest used guest
 * BAR3 addresses here -> the IO_PAGE_FAULTs at 0x8005..0x8007xxxx: the trace never had a working tank.)
 */
static bool tank_bar3 = true;
module_param(tank_bar3, bool, 0444);
MODULE_PARM_DESC(tank_bar3, "Windows mode: device pages 0x1000-0x1FFFFF (the tank) in the card's own memory (BAR3), as Windows");
#define TANK_BAR3_FIRST	0x1		/* device page */
#define TANK_BAR3_LAST	0x1FF

#define XF_CHUNKS	32
#define XF_CHUNK	(1024 * 1024)
static void *xf_cpu[XF_CHUNKS];
static dma_addr_t xf_dma[XF_CHUNKS];

void *ct_dsp_fx_audio_buf(u32 *devaddr, size_t *size)
{
        if (!xfer_buf_cpu[XB_AUDIO])
                return NULL;
        *devaddr = WIN_PLAY_VA;
        *size = xfer_buf_size[XB_AUDIO];
        return xfer_buf_cpu[XB_AUDIO];
}

void *ct_dsp_fx_rec_buf(u32 *devaddr, size_t *size)
{
	unsigned int c = WIN_REC_VA / XF_CHUNK;

	if (!xf_cpu[c])
		return NULL;
	*devaddr = WIN_REC_VA;
	*size = WIN_REC_BYTES;
	return (char *)xf_cpu[c] + WIN_REC_VA % XF_CHUNK;
}

int ct_dsp_fx_xfer_buf(unsigned int id, u64 *bus, void **cpu)
{
        if (id < XB_NUM) {
                if (!xfer_buf_cpu[id])
                        return -ENOENT;
                *bus = xfer_buf_dma[id];
                *cpu = xfer_buf_cpu[id];
                return 0;
        }
        if ((id == 20 || id == 21) && sysfs_atc) {
                *bus = pci_resource_start(sysfs_atc->pci, 3) + (id == 21 ? 0x100000 : 0);
                *cpu = NULL;
                return 0;
        }
        return -ENOENT;
}

int ct_dsp_fx_devpage(u32 dev, u64 *bus)
{
        unsigned int page = dev >> 12, pt = page / 512;

        if (pt >= 16 || !xfer_buf_cpu[XB_PT0 + pt])
                return -ERANGE;
        *bus = ((u64 *)xfer_buf_cpu[XB_PT0 + pt])[page % 512] + (dev & 0xFFF);
        return 0;
}

void ct_dsp_fx_loop(u32 *sa, u32 *bytes)
{
        *sa = WIN_PLAY_VA;
        *bytes = WIN_LOOP_BYTES;
}

static void xfer_bufs_free(void)
{
        for (int i = 0; i < XF_CHUNKS; i++) {
                if (xf_cpu[i] && sysfs_atc)
                        dma_free_coherent(&sysfs_atc->pci->dev, XF_CHUNK, xf_cpu[i], xf_dma[i]);
                xf_cpu[i] = NULL;
        }
        for (int i = 0; i < XB_NUM; i++) {
                if (xfer_buf_cpu[i] && sysfs_atc)
                        dma_free_coherent(&sysfs_atc->pci->dev, xfer_buf_size[i], xfer_buf_cpu[i], xfer_buf_dma[i]);
                xfer_buf_cpu[i] = NULL;
        }
}

static void pt_set(unsigned int devpage, dma_addr_t addr)
{
        unsigned int pt = devpage / 512, e = devpage % 512;

        if (pt < 16)
                ((u64 *)xfer_buf_cpu[XB_PT0 + pt])[e] = addr;
}

static void xfer_bufs_alloc(struct ct_atc *atc)
{
        static const char * const fixed[] = { "default", "buf72", "buf32", "audio" };
        size_t fixed_size[4];

        if (win_loop_frames < 96 || win_loop_frames > 16384)
                win_loop_frames = 960;
        win_loop_frames -= win_loop_frames % 48;
        fixed_size[0] = 4096;
        fixed_size[1] = 72 * 4096;
        fixed_size[2] = 32 * 4096;
        fixed_size[3] = WIN_AUDIO_BYTES;

        for (int i = 0; i < XB_NUM; i++) {
                if (i < XB_PT0) {
                        xfer_buf_size[i] = fixed_size[i];
                        strscpy(xfer_buf_name[i], fixed[i], sizeof(xfer_buf_name[i]));
                } else {
                        xfer_buf_size[i] = 4096;
                        snprintf(xfer_buf_name[i], sizeof(xfer_buf_name[i]), "pt%d", i - XB_PT0);
                }
                xfer_buf_cpu[i] = dma_alloc_coherent(&atc->pci->dev, xfer_buf_size[i], &xfer_buf_dma[i], GFP_KERNEL);
                if (!xfer_buf_cpu[i]) {
                        dev_warn(atc->card->dev, "X-Fi: transport buffer %s not allocated\n", xfer_buf_name[i]);
                        xfer_bufs_free();
                        return;
                }
        }
        /* page tables: everything -> default page, then Windows' known device pages */
        for (unsigned int p = 0; p < 16 * 512; p++)
                pt_set(p, xfer_buf_dma[XB_DEFAULT]);
        if (xfer_full_mem) {
                int n = 0;

                for (int c = 0; c < XF_CHUNKS; c++) {
                        xf_cpu[c] = dma_alloc_coherent(&atc->pci->dev, XF_CHUNK, &xf_dma[c], GFP_KERNEL);
                        if (!xf_cpu[c])
                                break;
                        for (unsigned int k = 0; k < XF_CHUNK / 4096; k++)
                                pt_set(c * (XF_CHUNK / 4096) + k, xf_dma[c] + k * 4096);
                        n++;
                }
                dev_info(atc->card->dev, "X-Fi: transport: %d MB of device pages backed by real memory\n", n);
        }
        if (tank_bar3 && pci_resource_len(atc->pci, 3) >= (TANK_BAR3_LAST + 1) * 4096) {
                resource_size_t bar3 = pci_resource_start(atc->pci, 3);

                for (unsigned int p = TANK_BAR3_FIRST; p <= TANK_BAR3_LAST; p++)
                        pt_set(p, bar3 + (p - 1) * 4096);
                dev_info(atc->card->dev, "X-Fi: transport: device 0x%x-0x%x (tank) -> card memory (BAR3 0x%llx)\n",
                         TANK_BAR3_FIRST << 12, ((TANK_BAR3_LAST + 1) << 12) - 1, (unsigned long long)bar3);
        }
        for (unsigned int k = 0; k < WIN_AUDIO_BYTES / 4096; k++)
                pt_set((WIN_PLAY_VA >> 12) + k, xfer_buf_dma[XB_AUDIO] + k * 4096);
        for (unsigned int k = 0; k < 72; k++)
                pt_set((0x481000 >> 12) + k, xfer_buf_dma[XB_BUF72] + k * 4096);
        for (unsigned int k = 0; k < 32; k++)
                pt_set((0x201000 >> 12) + k, xfer_buf_dma[XB_BUF32] + k * 4096);
        /* audio: silence (dma_alloc_coherent memory is zeroed). winmode_test_tone=1: a 1 kHz S32 stereo test tone
         * instead - the debugging signal that proved the Windows path before any player was involved; any loop of a
         * multiple of 48 frames is seamless (Windows' 960 = the default win_loop_frames). */
        if (!winmode_test_tone)
                return;
        for (unsigned int f = 0; f < WIN_AUDIO_BYTES / 8; f++) {
                ((s32 *)xfer_buf_cpu[XB_AUDIO])[2 * f] = tone_1k[f % 48];
                ((s32 *)xfer_buf_cpu[XB_AUDIO])[2 * f + 1] = tone_1k[f % 48];
        }
}

static ssize_t transport_bufs_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
        int n = 0;

        if (!sysfs_atc || !xfer_buf_cpu[0])
                return sysfs_emit(buf, "none\n");
        for (int i = 0; i < XB_NUM; i++)
                n += sysfs_emit_at(buf, n, "%s 0x%llx %zu\n", xfer_buf_name[i],
                                   (unsigned long long)xfer_buf_dma[i], xfer_buf_size[i]);
        n += sysfs_emit_at(buf, n, "loop 0x%x %u\n", WIN_PLAY_VA, WIN_LOOP_BYTES);
        n += sysfs_emit_at(buf, n, "bar3 0x%llx %llu\n",
                           (unsigned long long)pci_resource_start(sysfs_atc->pci, 3),
                           (unsigned long long)pci_resource_len(sysfs_atc->pci, 3));
        return n;
}

static struct kobj_attribute transport_bufs_attr = __ATTR(transport_bufs, 0444, transport_bufs_show, NULL);

/*
 * transport_mem: host access to the transport buffers' contents (Windows mode: the converter's address list at
 * SRCALBA = device 0x481000 = buf72 lives in memory, not registers). Writes:
 *   "fill NAME BASE"      every 32-bit word <- BASE + its byte offset (marker pattern)
 *   "poke NAME OFF VAL"   one 32-bit word
 *   "peek NAME OFF N"     reading the file then shows N words from OFF
 */
static int xmem_peek_buf = -1;
static size_t xmem_peek_off, xmem_peek_n;

static int xmem_find(const char *name)
{
        for (int i = 0; i < XB_NUM; i++)
                if (xfer_buf_cpu[i] && !strcmp(xfer_buf_name[i], name))
                        return i;
        return -1;
}

static ssize_t transport_mem_store(struct kobject *kobj, struct kobj_attribute *attr, const char *buf, size_t count)
{
        char cmd[8], name[12];
        unsigned long a = 0, b = 0;
        int i, n;
        u32 *w;

        n = sscanf(buf, "%7s %11s %li %li", cmd, name, &a, &b);
        if (n < 3 || (i = xmem_find(name)) < 0)
                return -EINVAL;
        w = xfer_buf_cpu[i];
        if (!strcmp(cmd, "fill")) {
                for (size_t o = 0; o < xfer_buf_size[i]; o += 4)
                        w[o / 4] = (u32)(a + o);
        } else if (!strcmp(cmd, "poke") && n == 4 && a + 4 <= xfer_buf_size[i] && !(a & 3)) {
                w[a / 4] = (u32)b;
        } else if (!strcmp(cmd, "peek") && n == 4 && !(a & 3) && a < xfer_buf_size[i]) {
                xmem_peek_buf = i;
                xmem_peek_off = a;
                xmem_peek_n = min_t(size_t, b, min_t(size_t, 256, (xfer_buf_size[i] - a) / 4));
        } else {
                return -EINVAL;
        }
        return count;
}

static ssize_t transport_mem_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
        int n = 0;

        if (xmem_peek_buf < 0 || !xfer_buf_cpu[xmem_peek_buf])
                return sysfs_emit(buf, "none\n");
        for (size_t k = 0; k < xmem_peek_n; k++)
                n += sysfs_emit_at(buf, n, "%#x %#010x\n", (unsigned int)(xmem_peek_off + 4 * k),
                                   ((u32 *)xfer_buf_cpu[xmem_peek_buf])[xmem_peek_off / 4 + k]);
        return n;
}

static struct kobj_attribute transport_mem_attr = __ATTR(transport_mem, 0600, transport_mem_show, transport_mem_store);

/*
 * regdump (root, read-only binary): file offset = register address, 4 bytes each, 0..0x1FFFFF. Only the areas Windows'
 * start writes are read (+ DSP RAM/core); everything else reads as 0. For comparing
 * the card's real state with Windows' (Entertainment mode debugging, 2026-10-30):
 *   sudo dd if=/sys/kernel/xfi_dsp/regdump of=dump.bin bs=64K
 */
static const u32 regdump_ranges[][2] = {
	{ 0x000000, 0x0C0000 }, { 0x100000, 0x10C000 }, { 0x110000, 0x114A00 }, { 0x130000, 0x13B440 },
	{ 0x140000, 0x15C000 }, { 0x16C000, 0x16D800 }, { 0x198000, 0x19C000 }, { 0x1B0000, 0x1C2000 },
	{ 0x1C5000, 0x1C6000 }, { 0x1C6060, 0x1C6074 }, { 0x1C7000, 0x1C7100 },
};

static bool regdump_readable(u32 reg)
{
	for (unsigned int i = 0; i < ARRAY_SIZE(regdump_ranges); i++)
		if (reg >= regdump_ranges[i][0] && reg < regdump_ranges[i][1])
			return true;
	return false;
}

static ssize_t regdump_read(struct file *f, struct kobject *k, const struct bin_attribute *a, char *buf,
			    loff_t off, size_t count)
{
	struct hw *hw = sysfs_atc ? sysfs_atc->hw : NULL;
	size_t n;

	if (!hw)
		return -ENODEV;
	if (off >= 0x200000)
		return 0;
	count = min_t(size_t, count, 0x200000 - off);
	for (n = 0; n + 4 <= count; n += 4) {
		u32 reg = (u32)(off + n) & ~3u;
		u32 v = regdump_readable(reg) ? dsp_reg_read(hw, reg) : 0;

		memcpy(buf + n, &v, 4);
		if (!(n & 0x3FFF))
			cond_resched();
	}
	return n;
}

static const struct bin_attribute regdump_attr = {
	.attr = { .name = "regdump", .mode = 0400 },
	.size = 0x200000,
	.read = regdump_read,
};

int ct_dsp_fx_sysfs_init(struct ct_atc *atc)
{
        int err;

        sysfs_atc = atc;
        xfi_kobj = kobject_create_and_add("xfi_dsp", kernel_kobj);
        if (!xfi_kobj)
                return -ENOMEM;

        err = sysfs_create_file(xfi_kobj, &reg_access_attr.attr);
        if (err) {
                kobject_put(xfi_kobj);
                xfi_kobj = NULL;
                return err;
        }
        if (atc->chip_type == ATC20K1) {
                xfer_bufs_alloc(atc);
                if (sysfs_create_file(xfi_kobj, &transport_bufs_attr.attr))
                        dev_warn(atc->card->dev, "X-Fi: transport_bufs sysfs file not created\n");
                if (sysfs_create_file(xfi_kobj, &transport_mem_attr.attr))
                        dev_warn(atc->card->dev, "X-Fi: transport_mem sysfs file not created\n");
                if (sysfs_create_bin_file(xfi_kobj, &regdump_attr))
                        dev_warn(atc->card->dev, "X-Fi: regdump sysfs file not created\n");
                if (ct_winmode_init(atc, xfi_kobj))
                        dev_warn(atc->card->dev, "X-Fi: Windows mode (winmode) not available\n");
        }
        return 0;
}

void ct_dsp_fx_sysfs_free(void)
{
        if (xfi_kobj) {
                ct_winmode_free(xfi_kobj);
                sysfs_remove_file(xfi_kobj, &transport_mem_attr.attr);
                sysfs_remove_bin_file(xfi_kobj, &regdump_attr);
                sysfs_remove_file(xfi_kobj, &transport_bufs_attr.attr);
                sysfs_remove_file(xfi_kobj, &reg_access_attr.attr);
                kobject_put(xfi_kobj);
                xfi_kobj = NULL;
        }
        for (int i = 0; i < 6; i++) {
                if (sysfs_bar[i] && sysfs_atc)
                        pci_iounmap(sysfs_atc->pci, sysfs_bar[i]);
                sysfs_bar[i] = NULL;
        }
        xfer_bufs_free();
        sysfs_atc = NULL;
}
