/* SPDX-License-Identifier: GPL-2.0 */
/* ALSA playback into Windows mode's SRC 202 loop (through the X-Fi DSP) - see ct_winplay.c */
#ifndef CT_WINPLAY_H
#define CT_WINPLAY_H

struct ct_atc;

int ct_winplay_create(struct ct_atc *atc);
unsigned int ct_winplay_master(void);		/* the master volume control's value (0..100) */

#endif /* CT_WINPLAY_H */
