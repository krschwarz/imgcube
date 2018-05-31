"""
Default FITS cube class. Reads in the data and generate the axes.

Things still to do:

    1 - slight difference in coordinates between CASA and here.

"""

import os
import numpy as np
from astropy.io import fits
import scipy.constants as sc
from astropy.convolution import Kernel
from astropy.convolution import convolve
from astropy.convolution import convolve_fft
from functions import percentiles_to_errors


class imagecube:

    msun = 1.988e30
    fwhm = 2. * np.sqrt(2 * np.log(2))

    def __init__(self, path, absolute=False, kelvin=True):
        """Load up an image cube."""

        # Read in the data and header.
        self.path = os.path.expanduser(path)
        self.fname = self.path.split('/')[-1]
        self.data = np.squeeze(fits.getdata(self.path))
        self.header = fits.getheader(path)

        # Generate the cube axes.
        self.absolute = absolute
        if self.absolute:
            print("Returning absolute coordinate values.")
            print("WARNING: self.dpix will be strange.")
        self.xaxis = self._readpositionaxis(a=1)
        self.yaxis = self._readpositionaxis(a=2)
        self.nxpix = self.xaxis.size
        self.nypix = self.yaxis.size
        self.dpix = np.mean([abs(np.diff(self.xaxis)),
                             abs(np.diff(self.yaxis))])

        self.velax = self._readvelocityaxis()

        # Get the beam properties of the beam.
        try:
            self.bmaj = self.header['bmaj'] * 3600.
            self.bmin = self.header['bmin'] * 3600.
            self.bpa = self.header['bpa']
            self.beamarea = self._calculate_beam_area_pix()
        except:
            self.bmaj = self.dpix
            self.bmin = self.dpix
            self.bpa = 0.0
            self.beamarea = self.dpix**2.0

        # Convert brightness to Kelvin if appropriate.
        self.nu = self._readrestfreq()
        self.bunit = self.header['bunit'].lower()
        if self.bunit == 'k':
            self.jy2k = 1.0
        else:
            self.jy2k = self._jy2k()
        if kelvin:
            if self.data.ndim == 2:
                print("WARNING: Converting to Kelvin.")
            self.data *= self.jy2k

        return

    # == Radial Profiles == #

    def radial_profile(self, rpnts=None, rbins=None, x0=0.0, y0=0.0,
                       inc=0.0, PA=0.0, collapse='max', statistic='mean',
                       PA_min=None, PA_max=None, exclude_PA=False,
                       beam_factor=False, clip_values=None):
        """
        Returns the azimuthally averaged intensity profile. If the data is 3D,
        then it is collapsed along the spectral axis.

        - Input -

        rpnts:              Bin centers in [arcsec] for the binning.
        rbins:              Bin edges in [arcsec] for the binning.
                            Note: Only specify either rpnts or rbins.
        x0, y0:             Source centre offset in [arcsec].
        inc, PA:            Inclination and position angle of the disk, both in
                            [degrees].
        collapse:           Method to collapse the cube: 'max', maximum value
                            along the spectral axis; 'sum', sum along the
                            spectral axis; 'int', integrated along the spectral
                            axis.
        statistic:          Return either the mean and standard deviation for
                            each annulus with 'mean' or the 16th, 50th and 84th
                            percentiles with 'percentiles'.
        PA_mask:            Only include values within [PA_min, PA_max].
        excxlude_PA_mask:   Exclude the values within [PA_min, PA_max]
        beam_factor:        Include the number of beams averaged over in the
                            calculation of the uncertainty.
        clip_values:        Clip values. If a single value is specified, clip
                            all absolute values below this, otherwise, if two
                            values are specified, clip values between these.

        - Output -

        pnts:               Array of bin centers.
        y:                  Array of the bin means or medians.
        dy:                 Array of uncertainties in the bin.
        """

        # Collapse the data to a 2D image if necessary.
        to_avg = self._collapse_cube(collapse).flatten()

        # Define the points to sample the radial profile at.
        rbins, rpnts = self._radial_sampling(rbins=rbins, rvals=rpnts)
        try:
            rvals = self.disk_coordinates_3D()[0].flatten()
        except:
            rvals = self.disk_coordinates(x0, y0, inc, PA)[0].flatten()

        # Apply the masks.
        mask = self._get_mask(r_min=rbins[0], r_max=rbins[-1], PA_min=PA_min,
                              PA_max=PA_max, exclude_PA=exclude_PA).flatten()
        if mask.size != to_avg.size:
            raise ValueError("Mask and data sizes do not match.")
        if clip_values is not None:
            clip_values = np.squeeze([clip_values])
            if clip_values.size == 1:
                mask *= abs(to_avg) >= clip_values
            else:
                mask *= np.logical_or(to_avg <= clip_values[0],
                                      to_avg >= clip_values[1])
        rvals, to_avg = rvals[mask], to_avg[mask]

        # Apply the averaging.
        ridxs = np.digitize(rvals, rbins)
        if statistic.lower() not in ['mean', 'percentiles']:
            raise ValueError("Must choose statistic: mean or percentiles.")
        if statistic.lower() == 'mean':
            y = [np.nanmean(to_avg[ridxs == r]) for r in range(1, rbins.size)]
            dy = [np.nanstd(to_avg[ridxs == r]) for r in range(1, rbins.size)]
            y, dy = np.squeeze(y), np.squeeze(dy)
        else:
            y = [np.nanpercentile(to_avg[ridxs == r], [16, 50, 84])
                 for r in range(1, rbins.size)]
            y = percentiles_to_errors(y)
            y, dy = y[0], y[1:]

        # Include the correction for the number of beams averaged over.
        if beam_factor:
            n_beams = 2. * np.pi * rpnts / self.bmaj
            PA_min = -np.pi if PA_min is None else PA_min
            PA_max = np.pi if PA_max is None else PA_max
            if PA_min != -np.pi or PA_max != np.pi:
                arc = (PA_max - PA_min) / 2. / np.pi
                arc = max(0.0, min(arc, 1.0))
                if exclude_PA:
                    n_beams *= 1. - arc
                else:
                    n_beams *= arc
            dy /= np.sqrt(n_beams)
        return rpnts, y, dy

    def _collapse_cube(self, method='max'):
        """Collapse the cube to a 2D image using the requested method."""
        if self.data.ndim > 2:
            if method.lower() not in ['max', 'sum', 'int']:
                raise ValueError("Must choose collpase method: max, sum, int.")
            if method.lower() == 'max':
                to_avg = np.amax(self.data, axis=0)
            elif method.lower() == 'sum':
                to_avg = np.nansum(self.data, axis=0)
            else:
                to_avg = np.where(np.isfinite(self.data), self.data, 0.0)
                to_avg = np.trapz(to_avg, self.velax, axis=0)
        else:
            to_avg = self.data.copy()
        return to_avg.flatten()

    def _radial_sampling(self, rbins=None, rvals=None):
        """Return default radial sampling if none are specified."""
        if rbins is not None and rvals is not None:
            raise ValueError("Specify only 'rbins' or 'rvals', not both.")
        if rvals is not None:
            dr = np.diff(rvals)[0] * 0.5
            rbins = np.linspace(rvals[0] - dr, rvals[-1] + dr, len(rvals) + 1)
        if rbins is not None:
            rvals = np.average([rbins[1:], rbins[:-1]], axis=0)
        else:
            rbins = np.arange(0, self.xaxis.max(), 0.25 * self.bmaj)[1:]
            rvals = np.average([rbins[1:], rbins[:-1]], axis=0)
        return rbins, rvals

    # == Functions to deal the synthesized beam. == #

    def _calculate_beam_area_str(self):
        """Beam area in steradians."""
        omega = np.radians(self.bmin / 3600.)
        omega *= np.radians(self.bmaj / 3600.)
        if self.bmin == self.dpix and self.bmaj == self.dpix:
            return omega
        return np.pi * omega / 4. / np.log(2.)

    def _calculate_beam_area_pix(self):
        """Beam area in pix^2."""
        omega = self.bmin * self.bmaj / np.power(self.dpix, 2)
        if self.bmin == self.dpix and self.bmaj == self.dpix:
            return omega
        return np.pi * omega / 2. / np.log(2.)

    @property
    def beam_per_pix(self):
        """Number of beams per pixel."""
        return self._calculate_beam_area_pix() / self.dpix**2

    @property
    def beam(self):
        """Returns the beam parameters in ["], ["], [deg]."""
        return self.bmaj, self.bmin, self.bpa

    def _beamkernel(self, bmaj=None, bmin=None, bpa=None, nbeams=1.0):
        """Returns the 2D Gaussian kernel for convolution."""
        if bmaj is None and bmin is None and bpa is None:
            bmaj = self.bmaj
            bmin = self.bmin
            bpa = self.bpa
        bmaj /= self.dpix * self.fwhm
        bmin /= self.dpix * self.fwhm
        bpa = np.radians(bpa)
        if nbeams > 1.0:
            bmin *= nbeams
            bmaj *= nbeams
        return Kernel(self._gaussian2D(bmin, bmaj, bpa + 90.).T)

    def _gaussian2D(self, dx, dy, PA=0.0):
        """2D Gaussian kernel in pixel coordinates."""
        xm = np.arange(-4*np.nanmax([dy, dx]), 4*np.nanmax([dy, dx])+1)
        x, y = np.meshgrid(xm, xm)
        x, y = self._rotate(x, y, PA)
        k = np.power(x / dx, 2) + np.power(y / dy, 2)
        return np.exp(-0.5 * k) / 2. / np.pi / dx / dy

    def _convolve_image(self, image, kernel, fast=True):
        """Convolve the image with the provided kernel."""
        if fast:
            return convolve_fft(image, kernel)
        return convolve(image, kernel)

    def convolve_cube(self, bmaj=None, bmin=None, bpa=None, nbeams=1.0,
                      fast=True, cube=None):
        """Convolve the cube with a 2D Gaussian beam."""
        if cube is None:
            cube = self.data
        kernel = self._beamkernel(bmaj=bmaj, bmin=bmin, bpa=bpa, nbeams=nbeams)
        convolved_cube = [self._convolve_image(c, kernel, fast) for c in cube]
        return np.squeeze(convolved_cube)

    # == Functions to write a Keplerian mask for CLEANing. == #

    def _keplerian_profile(self, x0=0.0, y0=0.0, inc=0.0, PA=0.0, mstar=1.0,
                           rout=None, rin=None, dist=100., vlsr=0.0):
        """Make a Keplerian mask for CLEANing."""

        # Pixel coordinates.
        rvals, tvals = self.disk_coordinates(x0, y0, inc, PA)
        rvals *= dist

        # Keplerian rotation profile.
        vkep = np.sqrt(sc.G * mstar * self.msun / rvals / sc.au)
        vkep *= np.sin(np.radians(inc)) * np.cos(tvals)
        vkep += vlsr

        # Mask non-disk regions.
        if rin is not None:
            vkep = np.where(rvals < rin, np.nan, vkep)
        if rout is not None:
            vkep = np.where(rvals > rout, np.nan, vkep)
        return vkep

    def _keplerian_mask(self, x0=0.0, y0=0.0, inc=0.0, PA=0.0, mstar=1.0,
                        rout=None, rin=None, dist=100, vlsr=0.0, dV=250.):
        """Generate the Keplerian mask as a cube. dV is FWHM of line."""
        mask = np.ones(self.data.shape) * self.velax[:, None, None]
        vkep = self._keplerian_profile(x0=x0, y0=y0, inc=inc, PA=PA,
                                       mstar=mstar, rout=rout, rin=rin,
                                       dist=dist, vlsr=vlsr)
        vkep = np.ones(self.data.shape) * vkep[None, :, :]
        return np.where(abs(mask - vkep) <= dV, 1., 0.)

    def write_keplerian_mask(self, x0=0.0, y0=0.0, inc=0.0, PA=0.0, mstar=1.0,
                             rout=None, rin=None, dist=100., vlsr=0.0, dV=250.,
                             nbeams=0.0):
        """Save a CASA readable mask using the spectral information."""
        mask = self._keplerian_mask(x0=x0, y0=y0, inc=inc, PA=PA, mstar=mstar,
                                    rout=rout, rin=rin, dist=dist, vlsr=vlsr,
                                    dV=dV)
        return mask

    # == Functions to deproject the pixel coordinates. == #

    def disk_coordinates(self, x0=0.0, y0=0.0, inc=0.0, PA=0.0):
        """
        Deprojected pixel coordinates in [arcsec, radians].
        Note that PA is relative to the eastern major axis.
        """
        x_sky, y_sky = np.meshgrid(self.xaxis[::-1] - x0, self.yaxis - y0)
        x_rot, y_rot = self._rotate(x_sky, y_sky, PA + 90.)
        x_dep, y_dep = self._incline(x_rot, y_rot, inc)
        return np.hypot(x_dep, y_dep), np.arctan2(y_dep, x_dep)

    def _rotate(self, x, y, PA):
        """Rotate (x, y) around the center by PA [deg]."""
        PArad = np.radians(PA + 90)
        x_rot = x * np.cos(PArad) + y * np.sin(PArad)
        y_rot = y * np.cos(PArad) - x * np.sin(PArad)
        return x_rot, y_rot

    def _incline(self, x, y, inc):
        """Incline (x, y) by inc [deg]."""
        return x, y / np.cos(np.radians(inc))

    # == Masking Functions == #

    def _get_mask(self, r_min=None, r_max=None, PA_min=None, PA_max=None,
                  exclude_r=False, exclude_PA=False, x0=0.0, y0=0.0, inc=0.0,
                  PA=0.0):
        """Returns a 2D mask for pixels in the given region."""
        try:
            rvals, tvals = self.disk_coordinates_3D(x0, y0, inc)
        except:
            rvals, tvals = self.disk_coordinates(x0, y0, inc, PA)
        r_min = rvals.min() if r_min is None else r_min
        r_max = rvals.max() if r_max is None else r_max
        PA_min = tvals.min() if PA_min is None else PA_min
        PA_max = tvals.max() if PA_max is None else PA_max
        r_mask = np.logical_and(rvals >= r_min, rvals <= r_max)
        PA_mask = np.logical_and(tvals >= PA_min, tvals <= PA_max)
        r_mask = ~r_mask if exclude_r else r_mask
        PA_mask = ~PA_mask if exclude_PA else PA_mask
        return r_mask * PA_mask

    # == Functions to read the data cube axes. == #

    def _readspectralaxis(self):
        """Returns the spectral axis in [Hz] or [m/s]."""
        a_len = self.header['naxis3']
        a_del = self.header['cdelt3']
        a_pix = self.header['crpix3']
        a_ref = self.header['crval3']
        return a_ref + (np.arange(a_len) - a_pix + 1.0) * a_del

    def _readpositionaxis(self, a=1):
        """Returns the position axis in [arcseconds]."""
        if a not in [1, 2]:
            raise ValueError("'a' must be in [0, 1].")
        a_len = self.header['naxis%d' % a]
        a_del = self.header['cdelt%d' % a]
        if a == 1 and self.absolute:
            a_del /= np.cos(np.radians(self.header['crval2']))
        a_pix = self.header['crpix%d' % a]
        a_ref = self.header['crval%d' % a]
        if not self.absolute:
            a_ref = 0.0
            a_pix -= 0.5
        axis = a_ref + (np.arange(a_len) - a_pix + 1.0) * a_del
        if self.absolute:
            return axis
        return 3600 * axis

    def _readrestfreq(self):
        """Read the rest frequency."""
        try:
            nu = self.header['restfreq']
        except KeyError:
            try:
                nu = self.header['restfrq']
            except KeyError:
                nu = self.header['crval3']
        return nu

    def _readvelocityaxis(self):
        """Wrapper for _velocityaxis and _spectralaxis."""
        if 'freq' in self.header['ctype3'].lower():
            specax = self._readspectralaxis()
            nu = self._readrestfreq()
            velax = (nu - specax) * sc.c / nu
        else:
            velax = self._readspectralaxis()
        return velax

    def _jy2k(self):
        """Jy/beam to K conversion."""
        jy2k = 1e-26 * sc.c**2 / self.nu**2 / 2. / sc.k
        return jy2k / self._calculate_beam_area_str()