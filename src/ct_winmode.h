/* SPDX-License-Identifier: GPL-2.0 */
/* Windows mode in the kernel: run the X-Fi DSP as Creative's Windows driver sets it up - see ct_winmode.c */
#ifndef CT_WINMODE_H
#define CT_WINMODE_H

#include <linux/types.h>

struct ct_atc;
struct kobject;

int ct_winmode_init(struct ct_atc *atc, struct kobject *kobj);
void ct_winmode_free(struct kobject *kobj);
/* system sleep: halt + unfreeze before, reload Windows mode after (ctatc.c atc_suspend / atc_resume) */
void ct_winmode_suspend(void);
void ct_winmode_resume(void);
int ct_winmode_create_controls(struct ct_atc *atc);
/* playback converter of the loaded firmware (SRC index; 202 before any firmware) - ct_winplay.c */
u32 ct_winmode_src(void);
/* channels of the playback stream of the loaded firmware (2 or 8) */
u32 ct_winmode_channels(void);
/* recording converter of the loaded firmware (0 = none) - ct_winrec.c */
u32 ct_winmode_cap_src(void);
/* recording the mic (not What U Hear): ct_winrec.c sends its full-level channel to both sides */
bool ct_winmode_rec_mono(void);
/* the firmware's master volume (Windows' volume slider, after the effects) - driven by ct_winplay's control */
#define WM_MASTER_NAME	"DSP Master Playback Volume"
int ct_winmode_set_master(unsigned int v);	/* -ENOENT: the firmware has none */
bool ct_winmode_has_master(void);
bool ct_winmode_running(void);
/* the card stopped answering (reads all ones, e.g. the card moved in its slot): stop touching it; any context */
void ct_winmode_card_gone(void);

#endif /* CT_WINMODE_H */
