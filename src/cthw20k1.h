/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Copyright (C) 2008, Creative Technology Ltd. All Rights Reserved.
 *
 * @File	cthw20k1.h
 *
 * @Brief
 * This file contains the definition of hardware access methord.
 *
 * @Author	Liu Chun
 * @Date 	May 13 2008
 */

#ifndef CTHW20K1_H
#define CTHW20K1_H

#include "cthardware.h"

int create_20k1_hw_obj(struct hw **rhw);
int destroy_20k1_hw_obj(struct hw *hw);
u32 ct20k1_reg_read(struct hw *hw, u32 reg);
void ct20k1_reg_write(struct hw *hw, u32 reg, u32 data);
void ct20k1_set_freeze(bool on);
int ct20k1_codec_write(struct hw *hw, u32 device, u32 reg, u32 data);
void ct20k1_mic_select(struct hw *hw);
void ct20k1_auto_mute(struct hw *hw, bool enabled);
void ct20k1_rear_mute(struct hw *hw, bool mute);

#endif /* CTHW20K1_H */
