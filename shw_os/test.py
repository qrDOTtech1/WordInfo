# JC4827W543 - Display + Touch test
# Flash this as main.py for first boot validation
# Should show colored corners, cross lines, slider (touch it!) and resolution text
import lvgl as lv
import task_handler
import lv_config

HEIGHT = lv_config.HEIGHT
WIDTH = lv_config.WIDTH

size_x = 50
size_y = 50

th = task_handler.TaskHandler()

print("SHW OS - Hardware Test")

scr = lv.screen_active()
scr.set_style_bg_color(lv.color_hex(0x000000), 0)

# Corner markers - Red top-left
rect = lv.obj(scr)
rect.set_size(size_x, size_y)
rect.set_pos(0, 0)
rect.set_style_bg_color(lv.color_make(255, 0, 0), 0)
label = lv.label(rect)
label.set_text("R")
label.center()
label.set_style_text_color(lv.color_make(0, 0, 0), 0)
label.set_style_text_font(lv.font_montserrat_16, 0)

# Green top-right
rect = lv.obj(scr)
rect.set_size(size_x, size_y)
rect.set_pos(WIDTH - size_x, 0)
rect.set_style_bg_color(lv.color_make(0, 255, 0), 0)
label = lv.label(rect)
label.set_text("G")
label.center()
label.set_style_text_color(lv.color_make(0, 0, 0), 0)
label.set_style_text_font(lv.font_montserrat_16, 0)

# Blue bottom-left
rect = lv.obj(scr)
rect.set_size(size_x, size_y)
rect.set_pos(0, HEIGHT - size_y)
rect.set_style_bg_color(lv.color_make(0, 0, 255), 0)
label = lv.label(rect)
label.set_text("B")
label.center()
label.set_style_text_color(lv.color_make(0, 0, 0), 0)
label.set_style_text_font(lv.font_montserrat_16, 0)

# White bottom-right
rect = lv.obj(scr)
rect.set_size(size_x, size_y)
rect.set_pos(WIDTH - size_x, HEIGHT - size_y)
rect.set_style_bg_color(lv.color_make(255, 255, 255), 0)
label = lv.label(rect)
label.set_text("W")
label.center()
label.set_style_text_color(lv.color_make(0, 0, 0), 0)
label.set_style_text_font(lv.font_montserrat_16, 0)

# Cross lines
line1 = lv.line(scr)
line2 = lv.line(scr)
points1 = [
    {"x": size_x, "y": size_y},
    {"x": WIDTH - size_x, "y": HEIGHT - size_y},
]
points2 = [
    {"x": WIDTH - size_x, "y": size_y},
    {"x": size_x, "y": HEIGHT - size_y},
]
line1.set_points(points1, 2)
line2.set_points(points2, 2)
line1.set_style_line_color(lv.color_make(255, 255, 255), 0)
line2.set_style_line_color(lv.color_make(255, 255, 255), 0)
line1.set_style_line_width(2, 0)
line2.set_style_line_width(2, 0)

# Slider to test touch
slider = lv.slider(scr)
slider.set_size(200, 40)
slider.center()

label = lv.label(scr)
label.set_text(f"{WIDTH}x{HEIGHT}")
label.align(lv.ALIGN.CENTER, 0, -100)
label.set_style_text_font(lv.font_montserrat_16, 0)

say_hello = lv.label(scr)
say_hello.set_text("SHW OS - Touch Test OK!")
say_hello.align(lv.ALIGN.CENTER, 0, 100)
say_hello.set_style_text_color(lv.color_make(0, 255, 0), 0)
