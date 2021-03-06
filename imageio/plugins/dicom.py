# -*- coding: utf-8 -*-
# Copyright (c) 2015, imageio contributors
# imageio is distributed under the terms of the (new) BSD License.

""" Plugin for reading DICOM files.
"""

# todo: Use pydicom:
# * Note: is not py3k ready yet
# * Allow reading the full meta info
# I think we can more or less replace the SimpleDicomReader with a
# pydicom.Dataset For series, only ned to read the full info from one
# file: speed still high
# * Perhaps allow writing?

from __future__ import absolute_import, print_function, division

import os
import sys
import subprocess

from .. import formats
from ..core import Format, BaseProgressIndicator, StdoutProgressIndicator
from ..core import read_n_bytes

_dicom = None  # lazily loaded in load_lib()


def load_lib():
    global _dicom
    from . import _dicom
    return _dicom


# Determine endianity of system
sys_is_little_endian = (sys.byteorder == 'little')


def get_dcmdjpeg_exe():
    fname = 'dcmdjpeg' + '.exe' * sys.platform.startswith('win')
    for dir in ('c:\\dcmtk',
                'c:\\Program Files', 'c:\\Program Files\\dcmtk',
                'c:\\Program Files (x86)\\dcmtk'
                ):
        filename = os.path.join(dir, fname)
        if os.path.isfile(filename):
            return filename
    
    try:
        subprocess.check_call([fname, '--version'], shell=True)
        return fname
    except Exception:
        return None


class DicomFormat(Format):
    """ A format for reading DICOM images: a common format used to store
    medical image data, such as X-ray, CT and MRI.
    
    This format borrows some code (and ideas) from the pydicom project,
    and (to the best of our knowledge) has the same limitations as
    pydicom with regard to the type of files that it can handle. However,
    only a predefined subset of tags are extracted from the file. This allows
    for great simplifications allowing us to make a stand-alone reader, and
    also results in a much faster read time. We plan to allow reading all
    tags in the future (by using pydicom).
    
    This format provides functionality to group images of the same
    series together, thus extracting volumes (and multiple volumes).
    Using volread will attempt to yield a volume. If multiple volumes
    are present, the first one is given. Using mimread will simply yield
    all images in the given directory (not taking series into account).
    
    Parameters for reading
    ----------------------
    progress : {True, False, BaseProgressIndicator}
        Whether to show progress when reading from multiple files.
        Default True. By passing an object that inherits from
        BaseProgressIndicator, the way in which progress is reported
        can be costumized.
    
    """
    
    def _can_read(self, request):
        # If user URI was a directory, we check whether it has a DICOM file
        if os.path.isdir(request.filename):
            files = os.listdir(request.filename)
            files.sort()  # Make it consistent
            if files:
                with open(os.path.join(request.filename, files[0]), 'rb') as f:
                    first_bytes = read_n_bytes(f, 140)
                return first_bytes[128:132] == b'DICM'
            else:
                return False
        # Check
        return request.firstbytes[128:132] == b'DICM'
    
    def _can_write(self, request):
        # We cannot save yet. May be possible if we will used pydicom as
        # a backend.
        return False
    
    # --
    
    class Reader(Format.Reader):
    
        def _open(self, progress=True):
            if not _dicom:
                load_lib()
            if os.path.isdir(self.request.filename):
                # A dir can be given if the user used the format explicitly
                self._info = {}
                self._data = None
            else:
                # Read the given dataset now ...
                try:
                    dcm = _dicom.SimpleDicomReader(self.request.get_file())
                except _dicom.CompressedDicom as err:
                    if 'JPEG' in str(err):
                        exe = get_dcmdjpeg_exe()
                        if not exe:
                            raise
                        fname1 = self.request.get_local_filename()
                        fname2 = fname1 + '.raw'
                        try:
                            subprocess.check_call([exe, fname1, fname2], shell=1)
                        except Exception:
                            raise err
                        print('DICOM file contained compressed data. '
                              'Used dcmtk to convert it.')
                        dcm = _dicom.SimpleDicomReader(fname2)
                    else:
                        raise
                
                self._info = dcm._info
                self._data = dcm.get_numpy_array()
            
            # Initialize series, list of DicomSeries objects
            self._series = None  # only created if needed
            
            # Set progress indicator
            if isinstance(progress, BaseProgressIndicator):
                self._progressIndicator = progress
            elif progress is True:
                p = StdoutProgressIndicator('Reading DICOM')
                self._progressIndicator = p
            elif progress in (None, False):
                self._progressIndicator = BaseProgressIndicator('Dummy')
            else:
                raise ValueError('Invalid value for progress.')
        
        def _close(self):
            # Clean up
            self._info = None
            self._data = None 
            self._series = None
        
        @property
        def series(self):
            if self._series is None:
                pi = self._progressIndicator
                self._series = _dicom.process_directory(self.request, pi)
            return self._series
        
        def _get_length(self):
            if self._data is None:
                dcm = self.series[0][0]
                self._info = dcm._info
                self._data = dcm.get_numpy_array()
            
            nslices = self._data.shape[0] if (self._data.ndim == 3) else 1
            
            if self.request.mode[1] == 'i':
                # User expects one, but lets be honest about this file
                return nslices
            elif self.request.mode[1] == 'I':
                # User expects multiple, if this file has multiple slices, ok.
                # Otherwise we have to check the series.
                if nslices > 1:
                    return nslices
                else:
                    return sum([len(serie) for serie in self.series])
            elif self.request.mode[1] == 'v':
                # User expects a volume, if this file has one, ok.
                # Otherwise we have to check the series
                if nslices > 1:
                    return 1
                else:
                    return len(self.series)  # We assume one volume per series
            elif self.request.mode[1] == 'V':
                # User expects multiple volumes. We have to check the series
                return len(self.series)  # We assume one volume per series
            else:
                raise RuntimeError('DICOM plugin should know what to expect.')
        
        def _get_data(self, index):
            if self._data is None:
                dcm = self.series[0][0]
                self._info = dcm._info
                self._data = dcm.get_numpy_array()
            
            nslices = self._data.shape[0] if (self._data.ndim == 3) else 1
            
            if self.request.mode[1] == 'i':
                # Allow index >1 only if this file contains >1
                if nslices > 1:
                    return self._data[index], self._info
                elif index == 0:
                    return self._data, self._info
                else:
                    raise IndexError('Dicom file contains only one slice.')
            elif self.request.mode[1] == 'I':
                # Return slice from volume, or return item from series
                if index == 0 and nslices > 1:
                    return self._data[index], self._info
                else:
                    L = []
                    for serie in self.series:
                        L.extend([dcm_ for dcm_ in serie])
                    return L[index].get_numpy_array(), L[index].info
            elif self.request.mode[1] in 'vV':
                # Return volume or series
                if index == 0 and nslices > 1:
                    return self._data, self._info
                else:
                    return (self.series[index].get_numpy_array(),
                            self.series[index].info)
            else:  # pragma: no cover
                raise ValueError('DICOM plugin should know what to expect.')
        
        def _get_meta_data(self, index):
            if self._data is None:
                dcm = self.series[0][0]
                self._info = dcm._info
                self._data = dcm.get_numpy_array()
            
            nslices = self._data.shape[0] if (self._data.ndim == 3) else 1
            
            # Default is the meta data of the given file, or the "first" file.
            if index is None:
                return self._info

            if self.request.mode[1] == 'i':
                return self._info
            elif self.request.mode[1] == 'I':
                # Return slice from volume, or return item from series
                if index == 0 and nslices > 1:
                    return self._info
                else:
                    L = []
                    for serie in self.series:
                        L.extend([dcm_ for dcm_ in serie])
                    return L[index].info
            elif self.request.mode[1] in 'vV':
                # Return volume or series
                if index == 0 and nslices > 1:
                    return self._info
                else:
                    return self.series[index].info
            else:  # pragma: no cover
                raise ValueError('DICOM plugin should know what to expect.')

# Add this format
formats.add_format(DicomFormat(
    'DICOM', 
    'Digital Imaging and Communications in Medicine', 
    '.dcm .ct .mri', 'iIvV'))  # Often DICOM files have weird or no extensions
