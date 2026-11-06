/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Creative X-Fi 20K1 DSP Effects Support
 * Reverse engineered from CTEXFIFX.sys
 */
#ifndef CT_DSP_FX_H
#define CT_DSP_FX_H

#include <linux/types.h>

struct ct_atc;
struct rsc;

/* Windows mode: the 'audio' transport buffer that converter SRC 202 loops (device address *devaddr) - ct_winplay.c */
void *ct_dsp_fx_audio_buf(u32 *devaddr, size_t *size);
/* Windows mode's recording loop (device addresses WIN_REC_VA..): CPU address, or NULL (needs xfer_full_mem) */
void *ct_dsp_fx_rec_buf(u32 *devaddr, size_t *size);
/* transport buffer by firmware buffer id (0 default, 1 buf72, 2 buf32, 3 audio, 4..19 pt0..15, 20 BAR3, 21 BAR3+1MB;
 * src/fw_blob.py): bus address, CPU address (NULL for BAR3). Returns 0, or -ENOENT if not allocated. */
int ct_dsp_fx_xfer_buf(unsigned int id, u64 *bus, void **cpu);
/* bus address behind device address dev, from our page tables (firmware DEVLO/DEVHI ops) */
int ct_dsp_fx_devpage(u32 dev, u64 *bus);
/* converter loop: device start address and length in bytes (win_loop_frames) */
void ct_dsp_fx_loop(u32 *sa, u32 *bytes);

/*
 * Debug capture of the effected DSP output. When the dsp_fx_capture_arc module param is nonzero,
 * atc_pcm_capture_prepare routes the capture's converting SRCs to read that DSP .output ARC (e.g. 0x120 = slot 2)
 * instead of the mixer front mix - so `arecord` records the effected audio. The capture path's bus-master MEMWR
 * recording SRC supplies the conversion clock the standalone dsp_route SRC never got. Returns 1 if it overrode the
 * source ports, 0 otherwise.
 */
int ct_dsp_fx_capture_override(struct ct_atc *atc, struct rsc **p0, struct rsc **p1);

enum ct_dsp_effect {
	CT_EFFECT_CRYSTALIZER = 0,
	CT_EFFECT_CMSS3D,
	CT_EFFECT_REVERB,
	CT_NUM_EFFECTS
};

enum ct_cmss3d_mode {
	CT_CMSS3D_HEADPHONE = 0,
	CT_CMSS3D_VIRTUAL,
	CT_CMSS3D_SURROUND
};

int  ct_dsp_fx_init(struct ct_atc *atc);
void ct_dsp_fx_free(struct ct_atc *atc);
void ct_dsp_fx_unroute(struct ct_atc *atc);
int  ct_dsp_fx_resume(struct ct_atc *atc);
int  ct_dsp_fx_create_controls(struct ct_atc *atc);
int  ct_dsp_fx_sysfs_init(struct ct_atc *atc);
void ct_dsp_fx_sysfs_free(void);

#endif /* CT_DSP_FX_H */
