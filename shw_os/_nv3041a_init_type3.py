# Copyright (c) 2024 - 2025 Kevin G. Schlosser
# From: https://github.com/straga/micropython_lcd
# NV3041A init sequence for JC4827W543

import time

import lcd_bus
import lvgl as lv
from micropython import const

_SLPOUT = const(0x11)
_INVON = const(0x21)
_DISPON = const(0x29)
_MADCTL = const(0x36)
_COLMOD = const(0x3A)
_FSM_VBP = const(0x44)
_FSM_VFP = const(0x45)
_FSM_HBP_ODD = const(0x46)
_FSM_HFP_ODD = const(0x47)
_FSM_HBP_EVEN = const(0x48)
_FSM_HFP_EVEN = const(0x49)
_GATE_ST_O = const(0x51)
_GATE_ED_O = const(0x52)
_GATE_ST_E = const(0x53)
_GATE_ED_E = const(0x54)
_SRC_LOAD = const(0x56)
_SRC_CS_ST = const(0x57)
_SRC_CS_PW = const(0x58)
_SRC_CS_NW = const(0x59)
_SRC_PC_ST_O = const(0x5A)
_SRC_PC_WD_O = const(0x5B)
_SRC_PC_ST_E = const(0x5C)
_SRC_PC_WD_E = const(0x5D)
_SRC_POL_SW = const(0x5E)
_SRC_OP_ST_O = const(0x60)
_SRC_OP_ST_E = const(0x61)
_SRC_OP_ED_MSB = const(0x62)
_SRC_OP_ED_O_LSB = const(0x63)
_SRC_OP_ED_E_LSB = const(0x64)
_SRC_OFC = const(0x65)
_SRC_IBIAS = const(0x67)
_USR_GVDD = const(0x6F)
_USR_GVCL = const(0x78)
_USR_VGSP = const(0x7A)
_VDDS_TRIM = const(0x7D)
_GAM_VRP0 = const(0x80)
_GAM_VRP1 = const(0x81)
_GAM_VRP2 = const(0x82)
_GAM_VRP3 = const(0x83)
_GAM_VRP4 = const(0x84)
_GAM_VRP5 = const(0x85)
_GAM_PRP0 = const(0x86)
_GAM_PRP1 = const(0x87)
_GAM_PKP0 = const(0x88)
_GAM_PKP1 = const(0x89)
_GAM_PKP2 = const(0x8A)
_GAM_PKP3 = const(0x8B)
_GAM_PKP4 = const(0x8C)
_GAM_PKP5 = const(0x8D)
_GAM_PKP6 = const(0x8E)
_GAM_PKP7 = const(0x8F)
_GAM_PKP8 = const(0x90)
_GAM_PKP9 = const(0x91)
_GAM_PKP10 = const(0x92)
_GAM_VRN0 = const(0xA0)
_GAM_VRN1 = const(0xA1)
_GAM_VRN2 = const(0xA2)
_GAM_VRN3 = const(0xA3)
_GAM_VRN4 = const(0xA4)
_GAM_VRN5 = const(0xA5)
_GAM_PRN0 = const(0xA6)
_GAM_PRN1 = const(0xA7)
_GAM_PKN0 = const(0xA8)
_GAM_PKN1 = const(0xA9)
_GAM_PKN2 = const(0xAA)
_GAM_PKN3 = const(0xAB)
_GAM_PKN4 = const(0xAC)
_GAM_PKN5 = const(0xAD)
_GAM_PKN6 = const(0xAE)
_GAM_PKN7 = const(0xAF)
_GAM_PKN8 = const(0xB0)
_GAM_PKN9 = const(0xB1)
_GAM_PKN10 = const(0xB2)
_MV_CLP = const(0xC1)
_VGH_CLP = const(0xC2)
_VGL_CLP = const(0xC3)
_RATIO_CTRL = const(0xC6)
_MV_PUMP_CLK = const(0xC7)
_HV_PUMP_CLK = const(0xC8)
_MV_CLK_CLP = const(0xC9)


def init(self):
    param_buf = bytearray(1)
    param_mv = memoryview(param_buf)

    color_size = lv.color_format_get_size(self._color_space)

    if color_size == 2:
        COLMOD = 0x01
    elif color_size == 3:
        COLMOD = 0x00
    else:
        raise RuntimeError(
            "NV3401A IC only supports "
            "lv.COLOR_FORMAT.RGB888 and lv.COLOR_FORMAT.RGB565"
        )

    param_buf[0] = 0xA5
    self.set_params(0xFF, param_mv[:1])

    param_buf[0] = 0xC0
    self.set_params(_MADCTL, param_mv[:1])

    param_buf[0] = COLMOD
    self.set_params(_COLMOD, param_mv[:1])

    param_mv[0] = 0x01
    self.set_params(_INVON, param_mv[:1])

    param_buf[0] = 0x15
    self.set_params(_FSM_VBP, param_mv[:1])
    param_buf[0] = 0x15
    self.set_params(_FSM_VFP, param_mv[:1])

    param_buf[0] = 0x03
    self.set_params(_VDDS_TRIM, param_mv[:1])

    param_buf[0] = 0xBB
    self.set_params(_MV_CLP, param_mv[:1])
    param_buf[0] = 0x05
    self.set_params(_VGH_CLP, param_mv[:1])
    param_buf[0] = 0x10
    self.set_params(_VGL_CLP, param_mv[:1])

    param_buf[0] = 0x3E
    self.set_params(_RATIO_CTRL, param_mv[:1])

    param_buf[0] = 0x25
    self.set_params(_MV_PUMP_CLK, param_mv[:1])
    param_buf[0] = 0x11
    self.set_params(_HV_PUMP_CLK, param_mv[:1])

    param_buf[0] = 0x5F
    self.set_params(_USR_VGSP, param_mv[:1])
    param_buf[0] = 0x44
    self.set_params(_USR_GVDD, param_mv[:1])
    param_buf[0] = 0x70
    self.set_params(_USR_GVCL, param_mv[:1])

    param_buf[0] = 0x00
    self.set_params(_MV_CLK_CLP, param_mv[:1])
    param_buf[0] = 0x21
    self.set_params(_SRC_IBIAS, param_mv[:1])

    param_buf[0] = 0x0A
    self.set_params(_GATE_ST_O, param_mv[:1])
    param_buf[0] = 0x0A
    self.set_params(_GATE_ST_E, param_mv[:1])
    param_buf[0] = 0x76
    self.set_params(_GATE_ED_O, param_mv[:1])
    param_buf[0] = 0x76
    self.set_params(_GATE_ED_E, param_mv[:1])

    param_buf[0] = 0x0A
    self.set_params(_FSM_HBP_ODD, param_mv[:1])
    param_buf[0] = 0x2A
    self.set_params(_FSM_HFP_ODD, param_mv[:1])
    param_buf[0] = 0x0A
    self.set_params(_FSM_HBP_EVEN, param_mv[:1])
    param_buf[0] = 0x1A
    self.set_params(_FSM_HFP_EVEN, param_mv[:1])

    param_buf[0] = 0x43
    self.set_params(_SRC_LOAD, param_mv[:1])
    param_buf[0] = 0x42
    self.set_params(_SRC_CS_ST, param_mv[:1])
    param_buf[0] = 0x3C
    self.set_params(_SRC_CS_PW, param_mv[:1])
    param_buf[0] = 0x64
    self.set_params(_SRC_CS_NW, param_mv[:1])

    param_buf[0] = 0x41
    self.set_params(_SRC_PC_ST_O, param_mv[:1])
    param_buf[0] = 0x02
    self.set_params(_SRC_PC_ST_E, param_mv[:1])
    param_buf[0] = 0x3C
    self.set_params(_SRC_PC_WD_O, param_mv[:1])
    param_buf[0] = 0x3C
    self.set_params(_SRC_PC_WD_E, param_mv[:1])

    param_buf[0] = 0x80
    self.set_params(_SRC_OP_ST_O, param_mv[:1])
    param_buf[0] = 0x3F
    self.set_params(_SRC_OP_ST_E, param_mv[:1])
    param_buf[0] = 0x21
    self.set_params(_SRC_OP_ED_MSB, param_mv[:1])
    param_buf[0] = 0x07
    self.set_params(_SRC_OP_ED_O_LSB, param_mv[:1])
    param_buf[0] = 0xE0
    self.set_params(_SRC_OP_ED_E_LSB, param_mv[:1])

    param_buf[0] = 0x1F
    self.set_params(_SRC_POL_SW, param_mv[:1])
    param_buf[0] = 0x02
    self.set_params(_SRC_OFC, param_mv[:1])

    param_buf[0] = 0x20
    self.set_params(0xCA, param_mv[:1])
    param_buf[0] = 0x52
    self.set_params(0xCB, param_mv[:1])
    param_buf[0] = 0x10
    self.set_params(0xCC, param_mv[:1])
    param_buf[0] = 0x42
    self.set_params(0xCD, param_mv[:1])

    param_buf[0] = 0x20
    self.set_params(0xD0, param_mv[:1])
    param_buf[0] = 0x52
    self.set_params(0xD1, param_mv[:1])
    param_buf[0] = 0x10
    self.set_params(0xD2, param_mv[:1])
    param_buf[0] = 0x42
    self.set_params(0xD3, param_mv[:1])
    param_buf[0] = 0x0A
    self.set_params(0xD4, param_mv[:1])
    param_buf[0] = 0x32
    self.set_params(0xD5, param_mv[:1])

    param_buf[0] = 0x00
    self.set_params(_GAM_VRP0, param_mv[:1])
    param_buf[0] = 0x07
    self.set_params(_GAM_VRP1, param_mv[:1])
    param_buf[0] = 0x02
    self.set_params(_GAM_VRP2, param_mv[:1])

    param_buf[0] = 0x00
    self.set_params(_GAM_VRN0, param_mv[:1])
    param_buf[0] = 0x06
    self.set_params(_GAM_VRN1, param_mv[:1])
    param_buf[0] = 0x01
    self.set_params(_GAM_VRN2, param_mv[:1])

    param_buf[0] = 0x11
    self.set_params(_GAM_PRP0, param_mv[:1])
    param_buf[0] = 0x27
    self.set_params(_GAM_PRP1, param_mv[:1])
    param_buf[0] = 0x37
    self.set_params(_GAM_VRP3, param_mv[:1])
    param_buf[0] = 0x35
    self.set_params(_GAM_VRP4, param_mv[:1])
    param_buf[0] = 0x3F
    self.set_params(_GAM_VRP5, param_mv[:1])

    param_buf[0] = 0x10
    self.set_params(_GAM_PRN0, param_mv[:1])
    param_buf[0] = 0x27
    self.set_params(_GAM_PRN1, param_mv[:1])
    param_buf[0] = 0x37
    self.set_params(_GAM_VRN3, param_mv[:1])
    param_buf[0] = 0x35
    self.set_params(_GAM_VRN4, param_mv[:1])
    param_buf[0] = 0x3F
    self.set_params(_GAM_VRN5, param_mv[:1])

    param_buf[0] = 0x0B
    self.set_params(_GAM_PKP0, param_mv[:1])
    param_buf[0] = 0x14
    self.set_params(_GAM_PKP1, param_mv[:1])
    param_buf[0] = 0x1A
    self.set_params(_GAM_PKP2, param_mv[:1])
    param_buf[0] = 0x0A
    self.set_params(_GAM_PKP3, param_mv[:1])
    param_buf[0] = 0x14
    self.set_params(_GAM_PKP4, param_mv[:1])
    param_buf[0] = 0x17
    self.set_params(_GAM_PKP5, param_mv[:1])
    param_buf[0] = 0x16
    self.set_params(_GAM_PKP6, param_mv[:1])
    param_buf[0] = 0x1B
    self.set_params(_GAM_PKP7, param_mv[:1])
    param_buf[0] = 0x04
    self.set_params(_GAM_PKP8, param_mv[:1])
    param_buf[0] = 0x0A
    self.set_params(_GAM_PKP9, param_mv[:1])
    param_buf[0] = 0x16
    self.set_params(_GAM_PKP10, param_mv[:1])

    param_buf[0] = 0x0B
    self.set_params(_GAM_PKN0, param_mv[:1])
    param_buf[0] = 0x14
    self.set_params(_GAM_PKN1, param_mv[:1])
    param_buf[0] = 0x1B
    self.set_params(_GAM_PKN2, param_mv[:1])
    param_buf[0] = 0x0A
    self.set_params(_GAM_PKN3, param_mv[:1])
    param_buf[0] = 0x08
    self.set_params(_GAM_PKN4, param_mv[:1])
    param_buf[0] = 0x07
    self.set_params(_GAM_PKN5, param_mv[:1])
    param_buf[0] = 0x06
    self.set_params(_GAM_PKN6, param_mv[:1])
    param_buf[0] = 0x07
    self.set_params(_GAM_PKN7, param_mv[:1])
    param_buf[0] = 0x04
    self.set_params(_GAM_PKN8, param_mv[:1])
    param_buf[0] = 0x0A
    self.set_params(_GAM_PKN9, param_mv[:1])
    param_buf[0] = 0x15
    self.set_params(_GAM_PKN10, param_mv[:1])

    param_buf[0] = 0x00
    self.set_params(0xFF, param_mv[:1])

    param_buf[0] = 0x00
    self.set_params(_SLPOUT, param_mv[:1])

    time.sleep_ms(120)

    param_buf[0] = 0x00
    self.set_params(_DISPON, param_mv[:1])

    time.sleep_ms(100)
