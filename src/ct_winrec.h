/* SPDX-License-Identifier: GPL-2.0 */
/* ALSA capture from Windows mode's recording converter (through the X-Fi mixer) - see ct_winrec.c */
#ifndef CT_WINREC_H
#define CT_WINREC_H

struct ct_atc;

int ct_winrec_create(struct ct_atc *atc);

#endif /* CT_WINREC_H */
