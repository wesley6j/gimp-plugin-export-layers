#-------------------------------------------------------------------------------
#
# This file is part of Export Layers.
#
# Copyright (C) 2013, 2014 khalim19 <khalim19@gmail.com>
# 
# Export Layers is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# Export Layers is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with Export Layers.  If not, see <http://www.gnu.org/licenses/>.
#
#-------------------------------------------------------------------------------

"""
This module:
* is the core of the plug-in
* defines a class that exports layers as individual images
* defines filter rules for layers
"""

#===============================================================================

from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division

str = unicode

#=============================================================================== 

import os
from collections import defaultdict

import gimp
import gimpenums

from export_layers.pylibgimpplugin import libfiles
from export_layers.pylibgimpplugin import pylibgimp
from export_layers.pylibgimpplugin import layerdata
from export_layers.pylibgimpplugin import objectfilter
from export_layers.pylibgimpplugin import progress

#===============================================================================

pdb = gimp.pdb

#===============================================================================

class ExportLayersError(Exception):
  pass


class ExportLayersCancelError(ExportLayersError):
  pass

#===============================================================================

class OverwriteHandler(object):
  
  """
  This class handles conflicting files using the specified `OverwriteChooser`
  class with the following choices available:
  
    * Replace
    * Skip
    * Rename new file
    * Rename existing file
    * Cancel
  """
  
  __OVERWRITE_MODES = REPLACE, SKIP, RENAME_NEW, RENAME_EXISTING, CANCEL = (0, 1, 2, 3, 4)
  
  @classmethod
  def handle(cls, filename, overwrite_chooser):
    should_skip = False
    
    if os.path.exists(filename):
      overwrite_chooser.choose(filename=os.path.basename(filename))
      if overwrite_chooser.overwrite_mode == cls.SKIP:
        should_skip = True
      elif overwrite_chooser.overwrite_mode == cls.REPLACE:
        # Nothing needs to be done here.
        pass
      elif overwrite_chooser.overwrite_mode in (cls.RENAME_NEW, cls.RENAME_EXISTING):
        uniq_filename = libfiles.uniquify_filename(filename)
        if overwrite_chooser.overwrite_mode == cls.RENAME_NEW:
          filename = uniq_filename
        else:
          os.rename(filename, uniq_filename)
      elif overwrite_chooser.overwrite_mode == cls.CANCEL:
        raise ExportLayersCancelError("cancelled")
    
    return should_skip, filename

#===============================================================================

class LayerFilterRules(object):
  
  @staticmethod
  def is_layer(layerdata_elem):
    return layerdata_elem.layer_type == layerdata_elem.LAYER
  
  @staticmethod
  def is_nonempty_group(layerdata_elem):
    return layerdata_elem.layer_type == layerdata_elem.NONEMPTY_GROUP
  
  @staticmethod
  def is_empty_group(layerdata_elem):
    return layerdata_elem.layer_type == layerdata_elem.EMPTY_GROUP
  
  @staticmethod
  def is_top_level(layerdata_elem):
    return layerdata_elem.level == 0
  
  @staticmethod
  def is_path_visible(layerdata_elem):
    return layerdata_elem.path_visible
  
  @staticmethod
  def has_file_extension(layerdata_elem):
    index_ = layerdata_elem.layer_name.rfind('.')
    return index_ != -1
  
  @staticmethod
  def has_matching_file_extension(layerdata_elem, file_extension):
    return layerdata_elem.get_file_extension() == file_extension.lower()
  
  @staticmethod
  def is_enclosed_in_square_brackets(layerdata_elem):
    return layerdata_elem.layer_name.startswith("[") and layerdata_elem.layer_name.endswith("]")
  
  @staticmethod
  def is_not_enclosed_in_square_brackets(layerdata_elem):
    return not LayerFilterRules.is_enclosed_in_square_brackets(layerdata_elem)

#===============================================================================

class LayerExporter(object):
  
  """
  This class:
  * exports layers as separate images
  * validates layer names
  
  Attributes:
  
  * `initial_run_mode` - The run mode to use for the first layer exported.
    For subsequent layers, `gimpenums.RUN_WITH_LAST_VALS` is used. If the file
    format in which the layer is exported to can't handle
    `gimpenums.RUN_WITH_LAST_VALS`, `gimpenums.RUN_INTERACTIVE` is used.
  
  * `image` - GIMP image to export layers from.
  
  * `main_settings` - `MainSettings` instance containing the main settings of
    the plug-in. This class treats them as read-only.
  
  * `overwrite_chooser` - `OverwriteChooser` instance that is invoked if a file
    with the same name already exists.
  
  * `progress_updater` - `ProgressUpdater` instance that indicates the number of
    layers exported. If no progress update is desired, pass None.
  
  * `should_stop` - Can be used to stop the export prematurely. If True,
    the export is stopped after exporting the currently processed layer.
  
  * `exported_layers` - List of layers that were successfully exported. Includes
    layers which were skipped (when files with the same names already exist).
  """
  
  _COPY_SUFFIX = " copy"
  __EXPORT_STATUSES = (
    _NOT_EXPORTED_YET, _EXPORT_SUCCESSFUL, _FORCE_INTERACTIVE,
    _USE_DEFAULT_FILE_EXTENSION
  ) = (0, 1, 2, 3)
  
  def __init__(self, initial_run_mode, image, main_settings, overwrite_chooser, progress_updater):
    
    self.initial_run_mode = initial_run_mode
    self.image = image
    self.main_settings = main_settings
    self.overwrite_chooser = overwrite_chooser
    self.progress_updater = progress_updater
    
    self.should_stop = False
    self._exported_layers = []
  
  @property
  def exported_layers(self):
    return self._exported_layers
  
  def export_layers(self):
    """
    Export layers as separate images from the specified image.
    """
    
    self._init_attributes()
    self._set_layer_filters()
    
    self._setup()
    try:
      self._export_layers()
    finally:
      self._cleanup()
  
  class _LayerFileExtensionProperties(object):
    """
    This class contains additional data about a file extension. The file
    extension is not specified in this class, it is specified as a key to the
    `_layer_file_extension_properties` dict.
    
    Attributes:
    
    * `is_valid` - If True, file extension is valid and can be used in filenames
      for the `pdb.gimp_file_save` procedure.
    
    * `processed_count` - Number of layers with the specific file extension that
      have already been exported.
    """
    
    def __init__(self):
      self.is_valid = True
      self.processed_count = 0
  
  def _init_attributes(self):
    self.should_stop = False
    
    self._exported_layers = []
    
    self._output_directory = self.main_settings['output_directory'].value
    self._default_file_extension = self.main_settings['file_extension'].value
    self._include_layer_path = self.main_settings['layer_groups_as_directories'].value
    
    self._image_copy = None
    self._layer_data = layerdata.LayerData(self.image, is_filtered=True)
    self._background_layerdata = []
    self._background_layer_merged = None
    self._empty_groups_layerdata = []
    
    
    if self.progress_updater is None:
      self.progress_updater = progress.ProgressUpdater(None)
    self.progress_updater.reset()
    
    self._layer_file_extension_properties = defaultdict(self._LayerFileExtensionProperties)
    self._default_file_extension = self._default_file_extension.lstrip('.').lower()
    self._current_file_extension = self._default_file_extension
    self._file_export_func = self._get_file_export_func(self._default_file_extension)
    self._current_layer_export_status = self._NOT_EXPORTED_YET
    self._is_current_layer_skipped = False
  
  def _set_layer_filters(self):
    """
    Set layer filters according to the main settings.
    
    Create a list of background layers (which are not exported, but are used
    during the layer processing).
    """
    
    self._layer_data.filter.add_subfilter(
      'layer_types', objectfilter.ObjectFilter(objectfilter.ObjectFilter.MATCH_ANY)
    )
    
    self._layer_data.filter['layer_types'].add_rule(LayerFilterRules.is_layer)
    
    if self.main_settings['merge_layer_groups'].value:
      self._layer_data.filter.add_rule(LayerFilterRules.is_top_level)
      self._layer_data.filter['layer_types'].add_rule(LayerFilterRules.is_nonempty_group)
    
    if self.main_settings['ignore_invisible'].value:
      self._layer_data.filter.add_rule(LayerFilterRules.is_path_visible)
    
    if self.main_settings['empty_directories'].value:
      self._layer_data.filter['layer_types'].add_rule(LayerFilterRules.is_empty_group)
    
    if (self.main_settings['square_bracketed_mode'].value ==
        self.main_settings['square_bracketed_mode'].options['normal']):
      for layerdata_elem in self._layer_data:
        self._remove_square_brackets(layerdata_elem)
    elif (self.main_settings['square_bracketed_mode'].value ==
        self.main_settings['square_bracketed_mode'].options['background']):
      with self._layer_data.filter.add_rule_temp(LayerFilterRules.is_enclosed_in_square_brackets):
        self._background_layerdata = list(self._layer_data)
      self._layer_data.filter.add_rule(LayerFilterRules.is_not_enclosed_in_square_brackets)
    elif (self.main_settings['square_bracketed_mode'].value ==
          self.main_settings['square_bracketed_mode'].options['ignore']):
      self._layer_data.filter.add_rule(LayerFilterRules.is_not_enclosed_in_square_brackets)
    elif (self.main_settings['square_bracketed_mode'].value ==
          self.main_settings['square_bracketed_mode'].options['ignore_other']):
      with self._layer_data.filter.add_rule_temp(LayerFilterRules.is_enclosed_in_square_brackets):
        square_bracketed_layers = list(self._layer_data)
      
      self._layer_data.filter.add_rule(LayerFilterRules.is_not_enclosed_in_square_brackets)
      
      for layerdata_elem in self._layer_data:
        self._add_square_brackets(layerdata_elem)
      
      for layerdata_elem in square_bracketed_layers:
        self._remove_square_brackets(layerdata_elem)
    
    if (self.main_settings['file_ext_mode'].value ==
        self.main_settings['file_ext_mode'].options['only_matching_file_extension']):
      self._layer_data.filter.add_rule(LayerFilterRules.has_matching_file_extension, self._default_file_extension)
  
  def _export_layers(self):
    with self._layer_data.filter['layer_types'].remove_rule_temp(LayerFilterRules.is_empty_group,
                                                                 raise_if_not_found=False):
      self.progress_updater.num_total_tasks = len(self._layer_data)
    
    libfiles.make_dirs(self._output_directory)
    
    for layerdata_elem in self._layer_data:
      if self.should_stop:
        raise ExportLayersCancelError("Export stopped by user.")
      
      if layerdata_elem.layer_type in (layerdata_elem.LAYER, layerdata_elem.NONEMPTY_GROUP):
        layer = layerdata_elem.layer
        layer_copy = self._process_layer(layer)
        
        layerdata_elem.validate_name()
        self._strip_file_extension(layerdata_elem)
        self._set_file_extension_and_update_file_export_func(layerdata_elem)
        self._layer_data.uniquify_layer_name(layerdata_elem, self._include_layer_path,
                                             place_before_file_extension=True)
        
        self._export(layerdata_elem, self._image_copy, layer_copy)
        if self._current_layer_export_status == self._USE_DEFAULT_FILE_EXTENSION:
          self._set_file_extension_and_update_file_export_func(layerdata_elem)
          self._layer_data.uniquify_layer_name(layerdata_elem, self._include_layer_path,
                                               place_before_file_extension=True)
          self._export(layerdata_elem, self._image_copy, layer_copy)
        
        self.progress_updater.update_tasks(1)
        if not self._is_current_layer_skipped:
          # Append the original layer, not the copy, since the copy is going to
          # be destroyed.
          self._exported_layers.append(layer)
          self._layer_file_extension_properties[self._current_file_extension].processed_count += 1
        pdb.gimp_image_remove_layer(self._image_copy, layer_copy)
      else:
        layerdata_elem.validate_name()
        self._layer_data.uniquify_layer_name(layerdata_elem, self._include_layer_path,
                                             place_before_file_extension=False)
        empty_directory = layerdata_elem.get_filepath(self._output_directory, self._include_layer_path)
        libfiles.make_dirs(empty_directory)
  
  def _setup(self):
    # Save context just in case. No need for undo groups or undo freeze here.
    pdb.gimp_context_push()
    # Perform subsequent operations on a new image so that the original image
    # and its soon-to-be exported layers are left intact.
    self._image_copy = pdb.gimp_image_new(self.image.width, self.image.height, gimpenums.RGB)
#    self._display_id = pdb.gimp_display_new(self._image_copy)
  
  def _cleanup(self):
#    pdb.gimp_display_delete(self._display_id)
    pdb.gimp_image_delete(self._image_copy)
    pdb.gimp_context_pop()
  
  def _remove_square_brackets(self, layerdata_elem):
    if layerdata_elem.layer_name.startswith("["):
      layerdata_elem.layer_name = layerdata_elem.layer_name[1:]
    if layerdata_elem.layer_name.endswith("]"):
      layerdata_elem.layer_name = layerdata_elem.layer_name[:-1]
  
  def _add_square_brackets(self, layerdata_elem):
    layerdata_elem.layer_name = "[" + layerdata_elem.layer_name + "]"
  
  def _process_layer(self, layer):
    if self._background_layerdata:
      for i, bg_layerdata in enumerate(self._background_layerdata):
        bg_layer_copy = pdb.gimp_layer_new_from_drawable(bg_layerdata.layer, self._image_copy)
        pdb.gimp_image_insert_layer(self._image_copy, bg_layer_copy, None, i)
        pdb.gimp_item_set_visible(bg_layer_copy, True)
        if pdb.gimp_item_is_group(bg_layer_copy):
          bg_layer_copy = pylibgimp.merge_layer_group(self._image_copy, bg_layer_copy)
      if self.main_settings['use_image_size'].value:
        self._background_layer_merged = pdb.gimp_image_merge_visible_layers(self._image_copy, gimpenums.CLIP_TO_IMAGE)
    
    layer_copy = pdb.gimp_layer_new_from_drawable(layer, self._image_copy)
    pdb.gimp_image_insert_layer(self._image_copy, layer_copy, None, 0)
    # This is necessary for file formats which flatten the image (such as JPG).
    pdb.gimp_item_set_visible(layer_copy, True)
    if pdb.gimp_item_is_group(layer_copy):
      layer_copy = pylibgimp.merge_layer_group(self._image_copy, layer_copy)
    
    if self.main_settings['ignore_layer_modes'].value:
      layer_copy.mode = gimpenums.NORMAL_MODE
    
    self._image_copy.active_layer = layer_copy
    
    layer_copy = self._crop_and_merge(layer_copy)
    
    self._image_copy.active_layer = layer_copy
    
    return layer_copy
  
  def _get_file_export_func(self, file_extension):
    if file_extension == "raw":
      # Raw format doesn't seem to work with `pdb.gimp_file_save`, hence the
      # special handling.
      return pdb.file_raw_save
    else:
      return pdb.gimp_file_save
  
  def _crop_and_merge(self, layer):
    if not self.main_settings['use_image_size'].value:
      pdb.gimp_image_resize_to_layers(self._image_copy)
      if self.main_settings['crop_to_background'].value:
        if self._background_layerdata:
          layer = pdb.gimp_image_merge_visible_layers(self._image_copy, gimpenums.CLIP_TO_IMAGE)
        if self.main_settings['autocrop'].value:
          pdb.plug_in_autocrop(self._image_copy, layer)
      else:
        if self.main_settings['autocrop'].value:
          pdb.plug_in_autocrop(self._image_copy, layer)
        if self._background_layerdata:
          layer = pdb.gimp_image_merge_visible_layers(self._image_copy, gimpenums.CLIP_TO_IMAGE)
    else:
      if self.main_settings['crop_to_background'].value and self._background_layer_merged is not None:
        if self.main_settings['autocrop'].value:
          self._image_copy.active_layer = self._background_layer_merged
          pdb.plug_in_autocrop_layer(self._image_copy, self._background_layer_merged)
          self._image_copy.active_layer = layer
      else:
        if self.main_settings['autocrop'].value:
          pdb.plug_in_autocrop_layer(self._image_copy, layer)
      
      if self._background_layerdata:
        layer = pdb.gimp_image_merge_visible_layers(self._image_copy, gimpenums.CLIP_TO_IMAGE)
      
      pdb.gimp_layer_resize_to_image_size(layer)
    
    return layer
  
  def _strip_file_extension(self, layerdata_elem):
    if self.main_settings['strip_mode'].value in (
         self.main_settings['strip_mode'].options['identical'],
         self.main_settings['strip_mode'].options['always']):
      file_extension = layerdata_elem.get_file_extension()
      if file_extension:
        if self.main_settings['strip_mode'].value == self.main_settings['strip_mode'].options['identical']:
          if file_extension == self._default_file_extension:
            layerdata_elem.set_file_extension(None)
        else:
          layerdata_elem.set_file_extension(None)
  
  def _set_file_extension_and_update_file_export_func(self, layerdata_elem):
    if (self.main_settings['file_ext_mode'].value ==
        self.main_settings['file_ext_mode'].options['use_as_file_extensions']):
      
      file_extension = layerdata_elem.get_file_extension()
      if file_extension and self._layer_file_extension_properties[file_extension].is_valid:
        self._current_file_extension = file_extension
      else:
        layerdata_elem.set_file_extension(self._default_file_extension)
        self._current_file_extension = self._default_file_extension
      
      self._file_export_func = self._get_file_export_func(self._current_file_extension)
      
    elif (self.main_settings['file_ext_mode'].value ==
          self.main_settings['file_ext_mode'].options['no_special_handling']):
      
      layerdata_elem.layer_name += '.' + self._default_file_extension
  
  def _get_run_mode(self):
    if self._layer_file_extension_properties[self._current_file_extension].is_valid:
      if self._layer_file_extension_properties[self._current_file_extension].processed_count == 0:
        return self.initial_run_mode
      else:
        return gimpenums.RUN_WITH_LAST_VALS
    else:
      return self.initial_run_mode
  
  def _export(self, layerdata_elem, image, layer):
    output_filename = layerdata_elem.get_filepath(self._output_directory, self._include_layer_path)
    self._is_current_layer_skipped, output_filename = OverwriteHandler.handle(output_filename, self.overwrite_chooser)
    self.progress_updater.update_text("Saving '" + output_filename + "'")
    
    if not self._is_current_layer_skipped:
      self._export_layer(self._file_export_func, image, layer, output_filename)
  
  def _export_layer(self, file_export_function, image, layer, output_filename):
    run_mode = self._get_run_mode()
    libfiles.make_dirs(os.path.dirname(output_filename))
    
    self._export_layer_once(file_export_function, run_mode,
                            image, layer, output_filename)
    
    if self._current_layer_export_status == self._FORCE_INTERACTIVE:
      self._export_layer_once(file_export_function, gimpenums.RUN_INTERACTIVE,
                              image, layer, output_filename)
  
  def _export_layer_once(self, file_export_function, run_mode,
                         image, layer, output_filename):
    
    self._current_layer_export_status = self._NOT_EXPORTED_YET
    
    try:
      file_export_function(image, layer, output_filename.encode(),
                           os.path.basename(output_filename).encode(),
                           run_mode=run_mode)
    except RuntimeError as e:
      # HACK: Since `RuntimeError` could indicate anything, including
      # `pdb.gimp_file_save` failure, this is the only way to intercept the
      # "cancel" operation.
      if "cancelled" in e.message.lower():
        raise ExportLayersCancelError(e.message)
      else:
        if self._current_file_extension != self._default_file_extension:
          self._layer_file_extension_properties[self._current_file_extension].is_valid = False
          self._current_file_extension = self._default_file_extension
          self._current_layer_export_status = self._USE_DEFAULT_FILE_EXTENSION
        else:
          # Try again, this time forcing the interactive mode if the
          # non-interactive mode failed (certain file types do not allow the
          # non-interactive mode).
          if run_mode in (gimpenums.RUN_WITH_LAST_VALS, gimpenums.RUN_NONINTERACTIVE):
            self._current_layer_export_status = self._FORCE_INTERACTIVE
          else:
            error_message = '"' + self._current_file_extension + '": ' + e.message
            if not e.message.endswith('.'):
              error_message += '.'
            raise ExportLayersError(error_message)
    else:
      self._current_layer_export_status = self._EXPORT_SUCCESSFUL
