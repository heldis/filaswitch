import logging
import re
from extruder import Extruder
from switch_tower import PEEK
from gcode import GCode, TYPE_CARTESIAN, TYPE_DELTA
from layer import FirstLayer, ACT_INFILL, ACT_PASS, ACT_SWITCH, Layer

import utils
from gcode_file import SLICER_PRUSA_SLIC3R, GCodeFile

gcode = GCode()
log = logging.getLogger("PrusaSlic3r")


class PrusaSlic3rCodeFile(GCodeFile):

    slicer_type = SLICER_PRUSA_SLIC3R

    LAYER_START_RE = re.compile(b"BEFORE_LAYER_CHANGE (\d+) (\d+\.*\d*)")
    VERSION_RE = re.compile(b".*(\d+)\.(\d+)\.(\d+)-prusa3d-.*")

    def __init__(self, logger, hw_config, tower_position, purge_lines):
        super().__init__(logger, hw_config, tower_position, purge_lines)

        self.extruder_use_wipe = []
        self.extruder_wipe = []
        self.relative_e = False
        self.retract_while_wiping = False
        self.version = None

        self.default_speed = None
        self.machine_type = None
        self.stroke_x = None
        self.stroke_y = None
        self.origin_offset_x = None
        self.origin_offset_y = None

    def process(self, gcode_file):
        self.open_file(gcode_file)
        self.parse_header()
        self.parse_print_settings()
        self.filter_layers()
        self.parse_perimeter_rates()
        if len(self.tools) > 1:
            self.find_tower_position()
            self.add_switch_raft()
            self.add_tool_change_gcode()
        else:
            self.log.info("No tool changes detected, skipping tool change g-code additions")
        return self.save_new_file()

    def parse_header(self):
        """
         Parse Prusa Slic3r header and stuff for print settings
        :return: none
        """

        z_offset = 0

        for layer in self.layers:
            for cmd, comment in layer.lines:
                if cmd:
                    continue
                if b"generated by Slic3r" in comment:
                    # parse version
                    try:
                        m = self.VERSION_RE.match(comment)
                        self.version = (int(m.groups()[0]), int(m.groups()[1]), int(m.groups()[2]))
                    except Exception as e:
                        print(e)
                elif b"bed_shape =" in comment:
                    #; bed_shape = 0x0,145x0,145x148,0x148
                    values = comment.split(b' = ')[1].split(b",")
                    if len(values) == 4:
                        self.machine_type = TYPE_CARTESIAN
                        self.origin_offset_x = -float(values[0].split(b"x")[0])
                        self.origin_offset_y = -float(values[0].split(b"x")[1])
                        self.stroke_x = float(values[2].split(b"x")[0]) + self.origin_offset_x
                        self.stroke_y = float(values[2].split(b"x")[1]) + self.origin_offset_y
                    else:
                        self.machine_type = TYPE_DELTA
                        x = []
                        y = []
                        for v in values:
                            vals = v.split(b"x")
                            x.append(float(vals[0]))
                            y.append(float(vals[1]))
                        self.stroke_x = max(x) - min(x)
                        self.stroke_y = max(y) - min(y)
                        self.origin_offset_x = self.stroke_x / 2
                        self.origin_offset_y = self.stroke_y / 2

                elif b"extrusion_multiplier =" in comment:
                    values = comment.split(b' = ')[1]
                    tool = 0
                    for d in values.split(b","):
                        if tool not in self.extruders:
                            self.extruders[tool] = Extruder(tool)
                        self.extruders[tool].feed_rate_multiplier = float(d)
                        tool += 1

                # elif b"external perimeters extrusion width" in comment:
                #     self.external_perimeter_widths.append(float(comment.split(b"=")[1:].strip()))
                # elif b"perimeters extrusion width" in comment:
                #     self.perimeter_widths.append(float(comment.split(b"=")[1:].strip()))
                # elif b"infill extrusion width" in comment:
                #     self.infill_widths.append(float(comment.split(b"=")[1:].strip()))
                # elif b"solid infill extrusion width" in comment:
                #     self.solid_infill_widths.append(float(comment.split(b"=")[1:].strip()))
                # elif b"top infill extrusion width" in comment:
                #     self.top_infill_widths.append(float(comment.split(b"=")[1:].strip()))

                elif b"filament_type =" in comment:
                    # ; filament_type = PLA;PLA;PLA;PLA
                    values = comment.split(b' = ')[1]
                    tool = 0
                    for d in values.split(b";"):
                        if tool not in self.extruders:
                            self.extruders[tool] = Extruder(tool)
                        self.extruders[tool].filament_type = d
                        tool += 1

                elif b"retract_length =" in comment:
                    #; retract_length = 3,3,3,3
                    values = comment.split(b' = ')[1]
                    tool = 0
                    for d in values.split(b","):
                        if tool not in self.extruders:
                            self.extruders[tool] = Extruder(tool)
                        self.extruders[tool].retract = float(d)
                        tool += 1

                elif b"retract_lift =" in comment:
                    # ; retract_lift = 0.5,0.5,0.5,0.5
                    values = comment.split(b' = ')[1]
                    tool = 0
                    for d in values.split(b","):
                        if tool not in self.extruders:
                            self.extruders[tool] = Extruder(tool)
                        self.extruders[tool].z_hop = float(d)
                        tool += 1

                elif b"retract_speed =" in comment:
                    # ; retract_speed = 80,80,80,80
                    values = comment.split(b' = ')[1]
                    tool = 0
                    for d in values.split(b","):
                        if tool not in self.extruders:
                            self.extruders[tool] = Extruder(tool)
                        self.extruders[tool].retract_speed = 60*float(d)
                        tool += 1

                elif b"use_relative_e_distances =" in comment:
                    # ; use_relative_e_distances = 1
                    if comment.split(b' = ')[1] != b"1":
                        raise ValueError("Relative E distances not enabled! Filaswitch won't work without relative E distances")

                elif b"wipe = " in comment:
                    # ; wipe = 1,1,1,1
                    values = comment.split(b' = ')[1]
                    tool = 0
                    for d in values.split(b","):
                        if tool not in self.extruders:
                            self.extruders[tool] = Extruder(tool)
                        if d == b"1":
                            self.extruders[tool].wipe = 4 # TODO: figure a way to read wipe length
                        tool += 1

                elif b"perimeter_speed =" in comment:
                    # ; perimeter_speed = 40
                    self.default_speed = float(comment.split(b' = ')[1]) * 60

                elif b"z_offset =" in comment:
                    # ; z_offset = 0
                    z_offset = float(comment.split(b' = ')[1])

                elif b"first_layer_speed =" in comment:
                    # ; first_layer_speed = 70%
                    self.first_layer_speed = float(comment.split(b' = ')[1].strip(b"%"))

                elif b"travel_speed =" in comment:
                    # ; travel_speed = 120
                    self.travel_xy_speed = float(comment.split(b' = ')[1]) * 60

                elif b" layer_height =" in comment:
                    # ; layer_height = 0.2
                    self.layer_height = float(comment.split(b' = ')[1])

                elif b"first_layer_temperature =" in comment:
                    #; first_layer_temperature = 215,195,215,215
                    values = comment.split(b' = ')[1]
                    tool = 0
                    for d in values.split(b","):
                        if tool not in self.extruders:
                            self.extruders[tool] = Extruder(tool)
                        self.extruders[tool].temperature_nr = tool
                        self.extruders[tool].temperature_setpoints[1] = int(d)
                        tool += 1

                elif b" temperature =" in comment:
                    #; temperature = 215,195,215,215
                    values = comment.split(b' = ')[1]
                    tool = 0
                    for d in values.split(b","):
                        if tool not in self.extruders:
                            self.extruders[tool] = Extruder(tool)
                        self.extruders[tool].temperature_setpoints[2] = int(d)
                        tool += 1

        if self.layer_height != 0.2:
            raise ValueError("Layer height must be 0.2, Filaswitch does not support any other lauer height at the moment")

        if not self.version:
            self.log.warning("Could not detect Slic3r version. Use at your own risk!")
        else:
            self.log.info("Slic3r version %d.%d.%d" % self.version)

        self.outer_perimeter_speed = self.default_speed
        self.first_layer_speed = (self.first_layer_speed/100) * self.outer_perimeter_speed

        for t in self.extruders:
            self.extruders[t].z_offset = z_offset

        self.travel_z_speed = self.travel_xy_speed

    def parse_print_settings(self):
        """ Slic3r specific settings """

        super().parse_print_settings()
        for cmd, comment, line_index in self.layers[0].read_lines():
            # find first tool change and remove it if it's T0. No need to
            # do tool change as e already have T0 active
            if line_index > self.layers[0].start_gcode_end and cmd and gcode.is_tool_change(cmd) is not None:
                if gcode.last_match == 0:
                    self.layers[0].delete_line(line_index)
                else:
                    # fix Prusa slicer first tool change with comment
                    self.layers[0].insert_line(line_index, None, b"TOOL CHANGE")
                break

    def parse_layers(self, lines):
        """
        Go through the g-code and find layer start points.
        Store each layer to list.
        :return:
        """
        prev_layer = None
        prev_height = 0
        current_layer = FirstLayer(0, 0.2, 0.2)

        layer_start = False
        layer_num = 0
        layer_z = 0

        for line in lines:
            cmd, comment = gcode.read_gcode_line(line)
            if comment:
                ret = self.check_layer_change(comment, None)
                if ret:
                    layer_num, layer_z = ret
                    layer_start = True

            if cmd and layer_start:
                if gcode.is_z_move(cmd):
                    layer_start = False
                    if current_layer.num == 1 and layer_num == 0:
                        current_layer.z = layer_z
                    else:
                        if prev_layer:
                            prev_z = prev_layer.z
                        else:
                            prev_z = 0

                        height = current_layer.z - prev_z
                        if height:
                            prev_height = height
                        else:
                            height = prev_height

                        self.layers.append(current_layer)
                        prev_layer = current_layer
                        current_layer = Layer(layer_num, layer_z, height)
            current_layer.add_line(cmd, comment)

        # last layer
        self.layers.append(current_layer)

    def check_layer_change(self, line, current_layer):
        """
        Check if line is layer change
        :param line: g-code line
        :param current_layer: current layer data
        :return: None or tuple of layer nr and layer z
        """
        m = self.LAYER_START_RE.match(line)
        if m:
            return int(m.groups()[0]), float(m.groups()[1])
        return current_layer

    def filter_layers(self):
        """
        Filter layers so that only layers relevant purge tower processing
        are returned. Also layers are tagged for action (tool witch, infill, pass)
        Layers that are left out:
        - empty (no command lines)
        - non-tool
        :return: none
        """

        # maxes = [last_switch_heights[k] for k in last_switch_heights]
        # maxes.sort(reverse=True)
        # last_switch_height = maxes[1]
        # print(last_switch_heights)
        # print(last_switch_height)

        layer_data = {}

        # step 1: filter out empty layers and add populate dictionary
        for layer in self.layers:
            if layer.z not in layer_data:
                layer_data[layer.z] = {'layers': []}

            layer_data[layer.z]['layers'].append(layer)

        # z-list sorted
        zs = sorted(layer_data.keys())

        # get needed slot counts per layer by going through reversed z-position list.
        # if there are only few changes for slot, tuck them in the previous slot
        slots = 1
        zs.reverse()
        count = 0
        for z in zs:
            lrs = 0
            for l in layer_data[z]['layers']:
                # each layer counts whether there's tool changes or not
                lrs += l.has_tool_changes()
            if lrs > slots:
                count += 1
            if count >= 3:
                slots = lrs
                count = 0
            layer_data[z]['slots'] = slots

        self.max_slots = slots
        #print(self.max_slots)

        # tag layers for actions: tool change, infill, etc
        zs.reverse()
        for z in zs:

            if layer_data[z]['slots'] == 0:
                continue

            slots_filled = 0
            # first check tool change layers
            for l in layer_data[z]['layers']:
                l.tower_slots = layer_data[z]['slots']
                if l.has_tool_changes():
                    l.action = ACT_SWITCH
                    slots_filled += 1

            # then check other layers
            for l in layer_data[z]['layers']:
                if not l.has_tool_changes():
                    if slots_filled < layer_data[z]['slots']:
                        l.action = ACT_INFILL
                        slots_filled += 1
                    else:
                        l.action = ACT_PASS

        # step 3: pack groups to list
        layers = []
        for z in zs:
            for l in layer_data[z]['layers']:
                layers.append(l)
        self.filtered_layers = sorted(layers, key=lambda x: x.num)

    def parse_perimeter_rates(self):
        """
        Parses perimeter print speed and feed rate for each layer
        :return: none
        """
        last_speed = None
        last_feed_rate = None
        for layer in self.layers:
            layer.outer_perimeter_speed = self.outer_perimeter_speed
            layer.outer_perimeter_feedrate = 0.05


if __name__ == "__main__":
    import logger
    logger = logger.Logger(".")
    s = PrusaSlic3rCodeFile(logger, PEEK, 4, "Automatic")
    print(s.check_layer_change(b" layer 1, Z = 1", None))