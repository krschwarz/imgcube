"""
Class for rotated cubes (with their major axis aligned with the xaxis.).
This has the functionaility to infer the emission surface following Pinte et
al (2018) with improvements.

TODO: Check how to recover the far side of the disk.
"""

import emcee
import celerite
import numpy as np
from cube import imagecube
from functions import offsetSHO
from functions import gaussian
from functions import sort_arrays
from functions import running_stdev
from functions import Matern32_model
from detect_peaks import detect_peaks
from scipy.interpolate import interp1d
from scipy.optimize import curve_fit, minimize


class rotatedcube(imagecube):

    def __init__(self, path, tilt='north', inc=None, mstar=None, dist=None,
                 x0=0.0, y0=0.0, verbose=True, suppress_warnings=True):
        """
        Read in the rotated image cube. The major axis must be aligned with the
        x-axis for the emission surface to be calculated.

        tilt:   Is the projected rotation axis (which should be along the
                y-axis) to the 'north' or to the 'south'.
        """

        # Initilize the class.
        imagecube.__init__(self, path, absolute=False, kelvin=True)
        if tilt.lower() not in ['north', 'south']:
            raise ValueError("Must specify tilt as 'north' or 'south'.")
        else:
            self.tilt = tilt.lower()
        self.verbose = verbose

        # Suppres warnings.
        if suppress_warnings:
            import warnings
            warnings.filterwarnings("ignore")

        # Get the deprojected pixel values assuming a thin disk.
        self.x0, self.y0 = x0, y0
        if inc is None:
            raise ValueError("WARNING: No inclination specified.")
        self.inc = inc
        if not 0 <= self.inc <= 90:
            raise ValueError("Inclination must be 0 <= i <= 90.")
        if dist is None:
            raise ValueError("WARNING: No distance specified.")
        self.dist = dist
        self.rdisk, self.tdisk = self.disk_coordinates(x0=self.x0, y0=self.y0,
                                                       inc=self.inc, PA=0.0)
        if mstar is None:
            raise ValueError("WARNING: No stellar mass specified.")
        else:
            self.mstar = mstar

        # Define the surface.
        self.rbins, self.rvals = self._radial_sampling()
        self.zvals = np.zeros(self.rvals.size)

        return

    # == Spectral Deprojection == #

    def _deprojected_width(self, vrot, spectra, angles, resample=True):
        """Return the width of the deprojected line profile."""
        x, y = self._deprojected_spectrum(spectra, angles, vrot, resample)
        Tb = np.max(y)
        dV = np.trapz(y, x) / Tb / np.sqrt(np.pi)
        x0 = x[y.argmax()]
        p0 = [Tb, dV, x0]
        try:
            return curve_fit(gaussian, x, y, p0=p0, maxfev=10000)[0][1]
        except:
            return 1e50

    def _deprojected_spectrum(self, spectra, angles, vrot, resample=True):
        """Collapsed deprojected spectrum."""
        deprojected = self._deproject_spectra(spectra, angles, vrot)
        if resample:
            return self.velax, np.nanmean(deprojected, axis=0)
        velax = self.velax[None, :] * np.ones(deprojected.shape)
        return sort_arrays(velax.flatten(), deprojected.flatten())

    def _deproject_spectra(self, spectra, angles, vrot):
        """Deproject all the spectra to a common systemic velocity."""
        deprojected = [interp1d(self.velax - vrot * np.cos(angle), spectrum,
                                fill_value='extrapolate')(self.velax)
                       for spectrum, angle in zip(spectra, angles)]
        return np.squeeze(deprojected)

    # == Rotation Profiles == #

    def get_rotation_profile(self, include_height=True, rbins=None, rpnts=None,
                             resample=True, PA_min=None, PA_max=None,
                             exclude_PA=False, method='dV', nwalkers=32,
                             nburnin=100, nsteps=100, plot_walkers=False,
                             plot_corner=False):
        """
        Return the rotation profile by deprojecting the spectra. Two methods
        are available: 'dV' and 'GP'.

        'dV' - minimizing the width. This works by assuming a Gaussian line
        profile for the deprojected line profile and finding the rotation
        velocity which minimizes this. This approach is fast, however does not
        allow for uncertainties to be calculated. It also has the implicity
        assumption that the deprojected line profile is Gaussian.

        'GP' - finding the smoothest model. This approach models the
        deprojected line profile as a Gaussian Process and tries to find the
        rotation velocity which results in the 'smoothest' model. This allows
        us to relax the assumption of a Gaussian line profile and also return
        uncertainties on the derived rotation velocity.

        - Inputs -

        rbins / rpnts:  Provide the radial grid in [arcsec] which you want to
                        bin the spectra into. By default this will span the
                        entire radius range.
        PA_min / max:
        resample:       When deprojecting the data, resample it onto the
                        velocity axis. If True this speeds up computation
                        considerably. TODO: Check whether this affects fitting.


        - Input -
        """

        # Check that the method is working.
        if method.lower() not in ['dv', 'gp']:
            raise ValueError("Must specify method: 'dV' or 'GP'.")
        if method.lower() == 'gp' and resample:
            print("WARNING: Resampling deprojected spectra does not work.")
            print("\t Setting resample = False.")
            resample = False

        # Deprojected pixel coordinates.
        if include_height:
            rvals, tvals = self.disk_coordinates_3D()
        else:
            rvals, tvals = self.disk_coordinates(self.x0, self.y0, self.inc)
        rvals, tvals = rvals.flatten(), tvals.flatten()
        if rbins is None and rvals is None:
            print("WARNING: No radial sampling set, this will take a while.")
        rbins, rpnts = self._radial_sampling(rbins=rbins, rvals=rpnts)

        # Flatten the data to [velocity, nxpix * nypix].
        dvals = self.data.copy().reshape(self.data.shape[0], -1)
        if dvals.shape != (self.velax.size, self.nxpix * self.nypix):
            raise ValueError("Wrong data shape.")

        # Cycle through each annulus and apply the method.
        v_rot = []
        for r in range(1, rbins.size):
            mask = self._get_mask(r_min=rbins[r-1], r_max=rbins[r],
                                  PA_min=PA_min, PA_max=PA_max,
                                  exclude_PA=exclude_PA).flatten()
            spectra, angles = dvals[:, mask].T, tvals[mask]

            if method.lower() == 'dV':
                v_rot += [self._get_vrot_from_width(spectra, angles, resample)]
            else:
                radius = rbins[r-1:r+1].mean()
                v_rot += [self._get_vrot_from_GP(spectra, angles, resample,
                                                 nwalkers, nburnin, nsteps,
                                                 plot_walkers, plot_corner,
                                                 self._projected_vkep(radius))]
        return rpnts, np.squeeze(v_rot)

    def _projected_vkep(self, radius, theta=None):
        """Return the projected Keplerian rotation at the given radius ["]."""
        try:
            import scipy.constants as sc
        except:
            raise ValueError("Cannot find scipy.constants.")
        vkep = sc.G * self.mstar * self.msun
        vkep = np.sqrt(vkep / radius / self.dist / sc.au)
        vkep *= 1.0 if theta is None else np.cos(theta)
        return vkep * np.sin(np.radians(self.inc))

    def _get_vrot_from_width(self, spectra, angles, resample=True):
        """Calculate rotation velocity by minimizing the linewidth."""
        vrot, vlsr = self._estimate_vrot(spectra, angles)
        args = (spectra, angles, resample)
        res = minimize(self._deprojected_width, vrot, args=args,
                       method='L-BFGS-B')
        return abs(res.x[0])

    def _estimate_vrot(self, spectra, angles):
        """Estimate the rotation velocity from fitting a SHO to peaks."""
        vpeaks = np.take(self.velax, np.argmax(spectra, axis=1))
        p0 = [0.5 * (np.max(vpeaks) - np.min(vpeaks)), np.mean(vpeaks)]
        try:
            popt, _ = curve_fit(offsetSHO, angles, vpeaks, p0=p0, maxfev=10000)
        except:
            popt = p0
        return np.squeeze(popt)

    def _get_p0(self, spectra, angles, nwalkers, scatter=3e-2):
        """
        Return starting positions for the GP approach. As a guide for the
        Gaussian Process model, rho ~ 3*dV, sigma ~ RMS.
        """

        # Guess the rotation velocity and systemic velocity.
        vrot, vlsr = self._estimate_vrot(spectra, angles)

        # Check that the rotation velocity is positive.
        if vrot < 0.0:
            raise ValueError("Blue shifted axis must be East for GP.")

        # Derive properties of the line.
        x, y = self._deprojected_spectrum(spectra, angles, vrot)
        dV = np.trapz(y, x) / y.max() / np.sqrt(2. * np.pi)
        rms = np.nanvar(self.data[0])

        # Include some scatter and return.
        p0 = np.array([vrot, rms, np.log(rms), np.log(dV)])
        print p0
        dp0 = np.random.randn(int(nwalkers * p0.size))
        dp0 = dp0.reshape((int(nwalkers), int(p0.size)))
        return p0[None, :] * (1.0 + scatter * dp0), vrot

    def _get_vrot_from_GP(self, spectra, angles, resample=False, nwalkers=32,
                          nburnin=100, nsteps=100, plot_walkers=False,
                          plot_corner=False, vkep=None):
        """Calculate rotation velocity by modelling lines as GPs."""
        p0, estimated_vrot = self._get_p0(spectra, angles, nwalkers)
        vkep = estimated_vrot if vkep is None else vkep
        sampler = emcee.EnsembleSampler(nwalkers, 4, self._log_probability_M32,
                                        args=(spectra, angles, resample, vkep))
        sampler.run_mcmc(p0, nburnin + nsteps)
        samples = sampler.chain[:, -nsteps:]
        samples = samples.reshape(-1, samples.shape[-1])
        if plot_walkers:
            self._plot_walkers(sampler.chain.T, nburnin)
        if plot_corner:
            self._plot_corner(samples)
        return np.percentile(samples, [16, 50, 84], axis=0)

    def _log_probability_M32(self, theta, spectra, angles, resample, vkep):
        """Log-probability function for the Gaussian Processes approach."""

        # Unpack the free parameters.
        vrot, noise, lnsigma, lnrho = theta

        # Uninformative priorsbut don't stray too far from the expected value.
        if abs(vrot - vkep) / vkep > 0.3:
            return -np.inf
        if noise <= 0.0:
            return -np.inf
        if not -5.0 < lnsigma < 10.:
            return -np.inf
        if not 0.0 <= lnrho <= 10.:
            return -np.inf

        # Generate the Gaussian Process model and return log-likelihood.
        x, y = self._deprojected_spectrum(spectra, angles, vrot, resample)
        k_noise = celerite.terms.JitterTerm(log_sigma=np.log(noise))
        k_line = celerite.terms.Matern32Term(log_sigma=lnsigma, log_rho=lnrho)
        kernel = k_noise + k_line
        gp = celerite.GP(kernel, mean=np.nanmean(y), fit_mean=True)
        try:
            gp.compute(x)
        except:
            return -np.inf
        ll = gp.log_likelihood(y, quiet=True)
        return ll if np.isfinite(ll) else -np.inf

    def _plot_walkers(self, samples, nburnin):
        """Plot the walkers to check if they are burning in."""
        try:
            import matplotlib.pyplot as plt
        except:
            raise ValueError("Cannot find matplotlib.")
        labels = [r'${\rm v_{rot}}$', r'${\rm \sigma_{rms}}$',
                  r'${\rm ln(\sigma)}$', r'${\rm ln(\rho)}$']
        for sample, label in zip(samples, labels):
            fig, ax = plt.subplots()
            for walker in sample.T:
                ax.plot(walker, alpha=0.1)
            ax.set_xlabel('Steps')
            ax.set_ylabel(label)
            ax.axvline(nburnin, ls=':', color='r')

    def _plot_corner(self, samples):
        """Plot the corner plot to check for covariances."""
        try:
            import corner
        except:
            raise ValueError("Cannont find corner.py")
        labels = [r'${\rm v_{rot}}$', r'${\rm \sigma_{rms}}$',
                  r'${\rm ln(\sigma)}$', r'${\rm ln(\rho)}$']
        corner.corner(samples, labels=labels, quantiles=[0.16, 0.5, 0.84],
                      show_titles=True)

    # == Spatial Deprojections == #

    def disk_coordinates_3D(self, niter=5):
        """
        Deprojected pixel coordinates in [arcsec, radians] taking account of
        the raised emission surface. Note that PA is relative to the eastern
        major axis.
        """
        rpix = None
        for _ in range(niter):
            rpix, tpix = self._disk_coordinates_3D_iteration(rpix)
        return rpix, tpix

    def _disk_coordinates_3D_iteration(self, rpix=None):
        """Return radius and position angle of each pixel."""
        if rpix is None:
            rpix = np.hypot(self.xaxis[None, :], self.yaxis[:, None])
        inc = np.radians(self.inc)
        zpix = self.emission_surface(rpix)
        zpix *= 1.0 if self.tilt == 'north' else -1.0
        ypix = np.ones(rpix.shape) * self.yaxis[:, None] / np.cos(inc)
        xpix = np.ones(rpix.shape) * self.xaxis[None, :]
        ypix -= zpix * np.tan(inc)
        return np.hypot(ypix, xpix), np.arctan2(ypix, xpix)

    # == Emission surface. == #

    def emission_surface(self, radii):
        """Linearlly interpolate the emission surface ["]."""
        if np.isnan(self.zvals[0]):
            idx = np.isfinite(self.zvals).argmax()
            rim = interp1d([0.0, self.rvals[idx]], [0.0, self.zvals[idx]])
            self.zvals[:idx] = rim(self.rvals[:idx])
        if np.isnan(self.zvals[-1]):
            idx = np.isnan(self.zvals).argmax()
            self.zvals[idx:] = 0.0
        return interp1d(self.rvals, self.zvals, bounds_error=False,
                        fill_value='extrapolate')(radii)

    def set_emission_surface_analytical(self, func='conical', theta=None):
        """
        Define the emission surface as an analytical function.

        - Input Variables -

        func:       Analytical function to use for the surface.
        theta:      Variables for the given function.

        - Possible Functions -

        powerlaw:   Power-law function: z = z_0 * (r / 1.0 arcsec)^z_q where
                    theta = [z_0, z_q].
        conical:    Flat, constant angle surface: z = r * tan(psi) + z_0, where
                    theta = [psi, z_0] where psi in [degrees].
        """
        theta = np.atleast_1d(theta)
        if func.lower() == 'powerlaw':
            if len(theta) != 2:
                raise ValueError("theta = [z_0, z_q].")
            self.zvals = theta[0] * np.power(self.rvals, theta[1])
        elif func.lower() == 'conical':
            if not 1 <= len(theta) < 3:
                raise ValueError("theta = [psi, (z_0)].")
            z0 = theta[1] if len(theta) == 2 else 0.0
            self.zvals = self.rvals * np.tan(np.radians(theta[0])) + z0
        else:
            raise ValueError("func must be 'powerlaw' or 'conical'.")
        return

    def set_emission_surface_data(self, nsigma=1.0, method='GP'):
        """Set the emission surface to that from the data."""
        r, z, _ = self.get_emission_surface_data(nsigma=nsigma, method=method)
        self.rvals, self.zvals = r, z

    def get_emission_surface_data(self, nsigma=1.0, method='GP', rbins=None,
                                  rvals=None):
        """
        Use the method in Pinte et al. (2018) to infer the emission surface.

        - Input Variables -

        x0, y0:     Coordinates [arcseconds] of the centre of the disk.
        inc         Inclination [degrees] of the disk.
        nsigma:     Clipping value used when removing background.

        - Output -

        coords:     A [3 x N] array where N is the number of successfully found
                    ellipses. Each ellipse yields a (r, z, Tb) trio. Distances
                    are in [au] (coverted using the provided distance) and the
                    brightness temperature in [K].
        """

        # Define the radial gridding.
        if rvals is None and rbins is None:
            rvals, rbins = self.rvals, self.rbins
        elif rvals is None:
            rvals = np.average([rbins[1:], rbins[:-1]], axis=0)
        elif rbins is None:
            dr = 0.5 * np.diff(rvals).mean()
            rbins = np.linspace(rvals[0]-dr, rvals[-1]+dr, rvals.size+1)
        clipped_data = self.data

        # Apply masking to the data.
        if nsigma > 0.0:
            r, I, dI = self.radial_profile(collapse='sum')
            mask = np.logical_and(I != 0.0, dI != 0.0)
            mask = nsigma * np.nanmean(dI[mask][-10])
            rsky = self.disk_coordinates(self.x0, self.y0, self.inc, 0.0)[0]
            mask = interp1d(r, I, fill_value='extrapolate')(rsky) >= mask
            mask = np.ones(clipped_data.shape) * mask[None, :, :]
            clipped_data = np.where(mask, clipped_data, 0.0)

            # Use the radial brightness temperature profile as another mask.
            r, Tb, dTb = self.radial_profile(collapse='max', beam_factor=False)
            clip = interp1d(r, Tb - nsigma * Tb, fill_value='extrapolate')
            r = self.disk_coordinates(self.x0, self.y0, self.inc, 0.0)[0]
            clipped_data = np.where(self.data >= clip(r), clipped_data, 0.0)

        # Calculate the emission surface and bin appropriately.
        r, z, Tb = self._get_emission_surface(clipped_data, self.x0, self.y0,
                                              self.inc, r_max=1.41*rbins[-1])
        if method.lower() not in ['gp', 'binned', 'raw']:
            raise ValueError("method must be 'gp', 'binned' or None.")
        if method.lower() == 'gp':
            r, z = sort_arrays(r, z)
            dz = running_stdev(z, window=(self.bmaj / np.nanmean(np.diff(r))))
            r, z, dz = Matern32_model(r, z, dz, jitter=True, return_var=True)
            z = interp1d(r, z, fill_value=np.nan, bounds_error=False)(rvals)
            dz = interp1d(r, dz, fill_value=np.nan, bounds_error=False)(rvals)
        elif method.lower() == 'binned':
            ridxs = np.digitize(r, rbins)
            dz = [np.nanstd(z[ridxs == rr]) for rr in range(1, rbins.size)]
            z = [np.nanmean(z[ridxs == rr]) for rr in range(1, rbins.size)]
            z, dz = np.squeeze(z), np.squeeze(dz)
        else:
            r, z = sort_arrays(r, z)
            dz = running_stdev(z, window=(self.bmaj / np.nanmean(np.diff(r))))
        return rvals, z, dz

    def plot_emission_surface(self, ax=None):
        """Plot the currently stored emission surface."""
        try:
            import matplotlib.pyplot as plt
        except:
            raise ValueError("Cannot find matplotlib.")
        if ax is None:
            fig, ax = plt.subplots()
        ax.errorbar(self.rvals, self.emission_surface(self.rvals),
                    fmt='-o', mew=0, color='k', ms=2)
        ax.set_xlim(0.0, self.rvals[self.zvals > 0.0].max()+self.bmaj)

    def _get_emission_surface(self, data, x0, y0, inc, r_max=None):
        """Find the emission surface [r, z, dz] values."""

        coords = []
        r_max = abs(self.xaxis).max() if r_max is None else r_max
        for c, channel in enumerate(data):

            # Avoid empty channels.
            if np.nanmax(channel) <= 0.0:
                continue

            # Cycle through the columns in the channel.
            for xidx in range(self.nxpix):

                # Skip rows if appropriate.
                if abs(self.xaxis[xidx] - x0) > r_max:
                    continue
                if np.nanmax(channel[:, xidx]) <= 0.0:
                    continue

                # Find the indices of the two largest peaks.
                yidx = detect_peaks(channel[:, xidx])
                if len(yidx) < 2:
                    continue
                pidx = channel[yidx, xidx].argsort()[::-1]
                yidx = yidx[pidx][:2]

                # Convert indices to polar coordinates.
                x = self.xaxis[xidx]
                yf, yn = self.yaxis[yidx]
                yc = 0.5 * (yf + yn)
                dy = max(yf - yc, yn - yc) / np.cos(np.radians(inc))
                r = np.hypot(x - x0, dy)
                z = abs(yc - y0) / np.sin(np.radians(inc))

                # Add coordinates to list. Apply some filtering.
                if np.isnan(r) or np.isnan(z) or z > r / 2.:
                    continue

                # Include the brightness temperature.
                Tb = channel[yidx[0], xidx]

                # Include the coordinates to the list.
                coords += [[r, z, Tb]]
        return np.squeeze(coords).T