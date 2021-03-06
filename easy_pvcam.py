# -*- coding: utf-8 -*-
"""
Class for user frendly use of a Princeton Instruments camera with PVCAM

Uses Princeton class from https://github.com/ColinBrosseau/pvcam_PrincetonInstruments_python

:Author:
  Colin-N. Brosseau

:Organization:
  Laboratoire Richard Leonelli, Universite de Montreal, Quebec

:Version: 2017.09

"""
from Princeton_wrapper import Princeton, PrincetonError
from master_Header_wrapper import *
import matplotlib.pyplot as plt
import numpy as np
import yaml
import spikes


class Easy_pvcam(Princeton):
    def __init__(self, number=0):
        super(Easy_pvcam, self).__init__(number = number)
        
        # Reinitialise
        # Without that, the second time one calls self.takePicture() it'll crash
        self.takePicture()
        try:
            self.takePicture()
        except OSError:
            self.closeCamera()
        super(Easy_pvcam, self).__init__(number = number)        
        
        chip_name = self.getParameterCurrentValue('CHIP_NAME').decode('UTF-8').replace(' ','')

        # import cameras configuration        
        with open("easy_pvcam.yaml", 'r') as ymlfile:
            camera_cfg = yaml.load(ymlfile)

        # DEFAULTS
        # Default temperature setpoint for safety if not present in configuration file  
        try:       
            self.setpoint_temperature = 20
        except PrincetonError:
            self.setpoint_temperature = -25
        # Default Signal corrections        
        self.__cosmic_peaks_spatial = None  # None, [0-100] Correct pixel above some threshold from neighbor mean value
        self.__cosmic_peaks_sequential = None
        #By default, camera is in full frame mode, set it to spectroscopy mode
        #set camera to 1D (vertical binning) acquisition
        self.setSpectroscopy()
        
        # Set camera parameters
        # Temperature (celcius)
        try:
            self.setpoint_temperature = camera_cfg[chip_name]['setpoint_temperature']
        except KeyError:
            pass
        # ADC speed index
        try:
            self.speed = camera_cfg[chip_name]['speed']
        except KeyError:
            pass
        # ADC gain
        try:
            self.gain = camera_cfg[chip_name]['gain']  
        except KeyError:
            pass
        # Exposure time in second
        try:
            self.exposureTime = camera_cfg[chip_name]['exposureTime']  
        except KeyError:
            pass

        # Mecanical Shutter
        self._shutter_present = False
        try:
            if 'shutter' in camera_cfg[chip_name]:
                self._initShutter()  # initialise Logic Output to drive the shutter
                # Delay (second) for setting of a mecanical shutter
                self.delayShutter = camera_cfg[chip_name]['shutter']['delay']
                # Shutter configuration
                self._ShutterMode = {'closed':ShutterOpenMode[camera_cfg[chip_name]['shutter']['closed']], 'opened':ShutterOpenMode[camera_cfg[chip_name]['shutter']['opened']]}
                self.shutter = 'closed'  # Needed to put the shutter in a valid state
                self._shutter_present = True
        except KeyError:
            pass
        
    def setImage(self):
        self._ROI = []
        self.addExposureROI(self._ROIfull)

    def setSpectroscopy(self):
        self._ROI = []
        self.addExposureROI(self._ROIspectroscopy)

#   Typical measurement
    def measure(self, exposure=False, removeBackgound=False):
        if exposure:
            self.exposureTime = exposure / self.numberPicturesToTake

        if removeBackgound and not self._shutter_present:
            import warnings
            warnings.warn('Cannot remove background because shutter not present or configured.')

        if removeBackgound and self._shutter_present:
            # Measure background
            self.shutter = 'closed'
            background, metadata = self.takePicture()
            background = np.squeeze(background)
            # Measure signal + background
            self.shutter = 'opened'
            spectrum, metadata = self.takePicture()
            spectrum = np.squeeze(spectrum)
            metadata = metadata[0][0]             
            # Perform cosmic peak sequential correction
            if self.__cosmic_peaks_sequential:
                spectrum = spikes.cleanSpikes(spectrum)
                background = spikes.cleanSpikes(background) 
            # Calculate signal without background
            spectrum = spectrum - background
        else: 
            # Measure signal + background
            spectrum, metadata = self.takePicture()
            spectrum = np.squeeze(spectrum)
            metadata = metadata[0][0]             
            # Perform cosmic peak sequential correction
            if self.__cosmic_peaks_sequential:
                spectrum = spikes.cleanSpikes(spectrum)
        
        # Correction cosmic_peaks_spatial
        self._correct_cosmic_peaks_spatial(spectrum[0])
        
        return spectrum, metadata
              
    # Exposure time (second)
    @property
    def exposureTime(self):
        """Get the exposure time in units given by EXP_RES."""
        PropertyFastExposureResolutionConstant = {0:1e-3,
            1:1e-6}
        factor = PropertyFastExposureResolutionConstant.get(self.getParameterCurrentValue('EXP_RES_INDEX'))
        expTime = self.getParameterCurrentValue('EXP_TIME')
        return expTime * factor
    
    @exposureTime.setter
    def exposureTime(self, exposure):
        """Set the exposure time.
        
        Parameters
        ----------
        exposureTime : exposure time in seconds 
                        unsigned int (0 - 65535)
        """
        if exposure < 0.065535:  #short exposure, microsecond resolution
            exposureUnits = ExposureUnits.microsecond
            self.expTime = int(exposure * 1e6)
        else:  #long exposure, millisecond resolution
            exposureUnits = ExposureUnits.millisecond
            self.expTime = int(exposure * 1e3)
                        
        self.setParameterValue('EXP_RES_INDEX', exposureUnits.value)
        self.setParameterValue('EXP_TIME', self.expTime)
       
    def close(self):
           self.closeCamera()
           
    # Shutter
    def _initShutter(self):
        self.logicOutput = LogicOutput.shutter
        
    @property
    def shutter(self):
        reverseShutterMode = {v.name:k for k, v in self._ShutterMode.items()}
        return reverseShutterMode[self.shutterOpenMode.name]

    @shutter.setter
    def shutter(self, value):
        exposure_Time = self.exposureTime  # Save exposure time
        self.shutterOpenMode = self._ShutterMode[value]
        # The shutter needs to have an exposure to apply.
        self.exposureTime = self.delayShutter  # delay to be sure the shutter is set 
        self.takePicture()
        # put back original exposure time
        self.exposureTime = exposure_Time  # Restore exposure time

    #==============================================================================
    #     Signal corrections 
    #==============================================================================
        
    # Cosmic Peaks removal
        
    # Spatial correction
    @property
    def cosmic_peaks_spatial(self):
        return self.__cosmic_peaks_spatial

    @cosmic_peaks_spatial.setter
    def cosmic_peaks_spatial(self, threshold):
        self.__cosmic_peaks_spatial = threshold
        
    def _correct_cosmic_peaks_spatial(self, spectrum):
        """
        Correct pixels from 'cosmic peaks' by comparing them with close neighborhood.
        """
        if self.cosmic_peaks_spatial:
            width = 2  # half width of neighborhood
            spectrum_pad = np.pad(spectrum, width, mode='edge')
            # Here width is hard coded i.e. [-2, -1, 1, 2]. Good idea to improve that. TODO
            # Median of values around each pixel
            median = np.median([np.roll(spectrum_pad, -2), np.roll(spectrum_pad, -1), np.roll(spectrum_pad, 1), np.roll(spectrum_pad, 2)], 0)[width:-width]
            # Index of problematic pixels
            I = np.abs((median-spectrum)/median) > self.__cosmic_peaks_spatial
            # Replace bad pixels with their neighborhood median
            spectrum[I] = median[I]     
        
    # Sequential correction
        
    @property
    def cosmic_peaks_sequential(self):
        return self.__cosmic_peaks_sequential

    @cosmic_peaks_sequential.setter
    def cosmic_peaks_sequential(self, value):
        assert isinstance(value, bool)
        if value:
            self.__cosmic_peaks_sequential = True
            if self.numberPicturesToTake < 5:
                self.numberPicturesToTake = 5
        else:
            self.__cosmic_peaks_sequential = False    

if __name__ == '__main__':
     camera = Easy_pvcam()
     print("ROI:")
     print(camera.ROI)
     print("Actual temperature (C):")
     print(camera.temperature)
     print("Setpoint temperature (C):")
     print(camera.setpoint_temperature)
     print("Gain index:")
     print(camera.gain)
     print("ADC speed index:")
     print(camera.speed)
     print("Camera Size:")
     print(camera.getCameraSize())
     print("Exposure time (second):")
     print(camera.exposureTime)
     print("Simple measurement")
     a = camera.measure(2)  # exposure time = 2 seconds
     plt.plot(a[0])
     print("Exposure parameters:")
     print(a[1])
     print("Simple measurement with backgound removal")
     a = camera.measure(2)  # exposure time = 2 seconds
     plt.plot(a[0])
     camera.close()
     
