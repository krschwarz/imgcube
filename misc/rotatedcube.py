"""
Class for rotated cubes (with their major axis aligned with the xaxis.).
This has the functionaility to infer the emission surface following Pinte et
al (2018) with improvements.
"""


import functions
import numpy as np
from cube import imagecube
import scipy.constants as sc
from detect_peaks import detect_peaks
from scipy.interpolate import interp1d


class rotatedcube(imagecube):

    def __init__(self, path, inc=None, mstar=None, dist=None, x0=0.0, y0=0.0,
                 clip=None, kelvin='RJ', verbose=True,
                 suppress_warnings=False):
        """Read in the rotated image cube."""

        # Initilize the class.
        imagecube.__init__(self, path, absolute=False, kelvin=kelvin,
                           clip=clip, verbose=verbose,
                           suppress_warnings=suppress_warnings)
        if not kelvin and self.verbose:
            print("WARNING: Not using Kelvin.")

        # Get the deprojected pixel values assuming a thin disk.
        self.PA = 270.
        self.x0, self.y0 = x0, y0
        if inc is None:
            raise ValueError("WARNING: No inclination specified.")
        self.inc = inc
        if not 0 <= self.inc <= 90:
            raise ValueError("Inclination must be 0 <= i <= 90.")
        if dist is None:
            raise ValueError("WARNING: No distance specified.")
        self.dist = dist
        if mstar is None:
            raise ValueError("WARNING: No stellar mass specified.")
        self.mstar = mstar
        self.rdisk, self.tdisk = self.disk_coords(self.x0, self.y0, self.inc)

        # Define the surface.
        self.nearest = 'north'
        self.tilt = 1.0
        self.rbins, self.rvals = self._radial_sampling()
        self.zvals = np.zeros(self.rvals.size)

        return

    # == Rotation Profiles == #

    def get_rotation_profile(self, rbins=None, rpnts=None, resample=True,
                             PA_min=None, PA_max=None, exclude_PA=False,
                             method='dV', beam_spacing=True, **kwargs):
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
        resample:       Average the points back down to the original
                        resolution. This will speed up the fitting but should
                        be used with caution.
        PA_min:         Minimum (relative) position angle to include.
        PA_max:         Maximum (relative) position angle to include.
        exclude_PA:     Exclude, rather than include PA_min < PA < PA_mask.
        method:         Which method to use, either 'dV' or 'GP'.
        beam_spacing:   Randomly draw spectra roughly a beamsize apart from the
                        annulus.

        - Output -

        rpnts:          The bin centres of the radial grid.
        v_rot:          If method='dV' then this is just v_rot in [m/s]. If
                        method='GP' this is the [16, 50, 84]th percentiles of
                        the posterior distribution for the GP model.
        """

        # Populate variables.

        try:
            from eddy.annulus import ensemble
        except:
            raise ValueError("Cannot find the eddy package.")
        if method.lower() not in ['dv', 'gp']:
            raise ValueError("Must specify method: 'dV' or 'GP'.")
        if method.lower() == 'gp' and resample:
            if self.verbose:
                print("WARNING: Resampling with GP method not advised.")

        # Default radial binning.

        if rbins is None and rpnts is None and self.verbose:
            print("WARNING: No radial sampling set, this will take a while.")
        rbins, rpnts = self._radial_sampling(rbins=rbins, rvals=rpnts)

        # Cycle through each annulus and apply the appropriate method.

        v_rot = []
        for r in range(1, rbins.size):

            if self.verbose:
                print("Running %d / %d..." % (r, rbins.size-1))

            # Get the annulus of points.

            spectra, theta = self.get_annulus(r_min=rbins[r-1], r_max=rbins[r],
                                              PA_min=PA_min, PA_max=PA_max,
                                              exclude_PA=exclude_PA,
                                              x0=self.x0, y0=self.y0,
                                              inc=self.inc, PA=90.,
                                              z_type='func',
                                              params=self.emission_surface,
                                              nearest=self.nearest,
                                              beam_spacing=beam_spacing,
                                              return_theta=True)

            # Create an ensemble instance from eddy if enough spectra (> 2).

            if len(theta) < 2:
                if self.verbose:
                    print("WARNING: Not enough spectra. Skipping annulus.")
                if method.lower() == 'dv':
                    v_rot += [np.nan]
                else:
                    v_rot += [np.nan * np.ones((3, 4))]
                continue

            # Check that there are some non-zero values.

            if np.nansum(spectra) == 0:
                if self.verbose:
                    print("WARNING: No positive values. Skipping annulus.")
                if method.lower() == 'dv':
                    v_rot += [np.nan]
                else:
                    v_rot += [np.nan * np.ones((3, 4))]
                continue

            annulus = ensemble(spectra=spectra, theta=theta, velax=self.velax,
                               suppress_warnings=0 if self.verbose else 1)

            # Infer the rotation velocity.

            v_kep = self.projected_vkep(rpnts[r-1])
            if method.lower() == 'dv':
                v_rot += [annulus.get_vrot_dV(vref=v_kep)]
            else:
                kwargs['return_all'] = True
                kwargs['plot_walkers'] = False
                kwargs['plot_corner'] = False
                try:
                    v_rot += [annulus.get_vrot_GP(vref=v_kep, **kwargs)]
                except:
                    v_rot += [np.zeros((3, 4))]

        return rpnts, np.squeeze(v_rot)

    def projected_vkep(self, rvals, theta=0.0):
        """Return the projected Keplerian rotation at the given radius ["]."""
        try:
            import scipy.constants as sc
        except:
            raise ValueError("Cannot find scipy.constants.")
        z = self.emission_surface(rvals) * self.dist * sc.au
        r = rvals * self.dist * sc.au
        vkep = sc.G * self.mstar * self.msun * np.power(r, 2.0)
        vkep = np.sqrt(vkep / np.power(np.hypot(r, z), 3.0))
        return vkep * np.sin(np.radians(self.inc)) * np.cos(theta)

    def fit_rotation_curve(self, rvals, vrot, dvrot=None, beam_clip=2.0,
                           fit_mstar=True, verbose=True, save=True):
        """Find the best fitting stellar mass for the rotation profile."""
        if beam_clip:
            mask = rvals > float(beam_clip) * self.bmaj
        else:
            mask = rvals > 0.0

        # Defining functions to let curve_fit do its thing.
        from scipy.optimize import curve_fit
        if fit_mstar:
            def vkep(rvals, mstar):
                return functions._keplerian(rvals, self.inc, mstar, self.dist)
            p0 = self.mstar
        else:
            def vkep(rvals, inc):
                return functions._keplerian(rvals, inc, self.mstar, self.dist)
            p0 = self.inc
        p, c = curve_fit(vkep, rvals[mask], vrot[mask], p0=p0, maxfev=10000,
                         sigma=dvrot[mask] if dvrot is not None else None)

        # Print, save and return the best-fit values.
        if fit_mstar:
            if verbose:
                print("Best-fit: Mstar = %.2f +\- %.2f Msun." % (p, c[0]))
            if save:
                self.mstar = p[0]
        else:
            if verbose:
                print("Best-fit inc: %.2f +\- %.2f degrees." % (p, c[0]))
            if save:
                self.inc = p[0]
        return p[0], c[0, 0]

    def _keplerian_mstar(self, rvals, mstar):
        """Keplerian rotation with stellar mass as free parameter."""
        vkep = np.sqrt(sc.G * mstar * self.msun / rvals / sc.au / self.dist)
        return vkep * np.sin(np.radians(self.inc))

    def _keplerian_inc(self, rvals, inc):
        """Keplerian rotation with inclination as free parameter."""
        vkep = sc.G * self.mstar * self.msun / rvals / sc.au / self.dist
        return np.sqrt(vkep * np.sin(np.radians(inc)))

    # == Emission surface. == #

    def emission_surface(self, radii):
        """Returns the height at the given radius for the stored height."""
        if np.isnan(self.zvals[0]):
            idx = np.isfinite(self.zvals).argmax()
            rim = interp1d([0.0, self.rvals[idx]], [0.0, self.zvals[idx]])
            self.zvals[:idx] = rim(self.rvals[:idx])
        if np.isnan(self.zvals[-1]):
            idx = np.isnan(self.zvals).argmax()
            self.zvals[idx:] = 0.0
        return interp1d(self.rvals, self.zvals, bounds_error=False,
                        fill_value='extrapolate')(radii)

    def set_emission_surface_analytical(self, z_type='flared',
                                        params=[0.3, 1.2], nearest=None):
        """
        Define the emission surface as an analytical function.

        - Input Variables -

        z_typr:     Analytical function to use for the surface.
        params:     Variables for the given function.

        - Possible Functions -

        flared:     Power-law function: z = z_0 * (r / 1.0 arcsec)^z_q where
                    theta = [z_0, z_q].
        conical:    Flat, constant angle surface: z = r * tan(psi) + z_0, where
                    theta = [psi, z_0] where psi in [degrees].

        """

        if nearest is None:
            raise ValueError("Must specifiy which side of the disk is closer.")
        if nearest not in ['north', 'south']:
            raise ValueError("Nearest must be 'north' or 'south'.")
        self.nearest = nearest
        self.tilt = 1.0 if nearest == 'north' else -1.0

        params = np.atleast_1d(params)
        if z_type.lower() == 'flared':
            if len(params) != 2:
                raise ValueError("theta = [z_0, z_q].")
            self.zvals = params[0] * np.power(self.rvals, params[1])
        elif z_type.lower() == 'conical':
            if not 1 <= len(params) < 3:
                raise ValueError("theta = [psi, (z_0)].")
            z0 = params[1] if len(params) == 2 else 0.0
            self.zvals = self.rvals * np.tan(np.radians(params[0])) + z0
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
                    ellipses. Each ellipse yields a (r, z, dz) trio. Distances
                    are in [au] (coverted using the provided distance) and the
                    brightness temperature in [K].
        """

        # Define the radial gridding.
        if rbins is None and rvals is None and self.verbose:
            print("WARNING: No radial sampling set, this may take a while.")
        rbins, rvals = self._radial_sampling(rbins=rbins, rvals=rvals)
        clipped_data = self.data

        # Apply masking to the data.
        if nsigma > 0.0:
            r, I, dI = self.radial_profile(collapse='sum')
            rsky = self.disk_coords(self.x0, self.y0, self.inc)[0]

            # Estimate the RMS.
            mask = np.logical_and(I != 0.0, dI != 0.0)
            mask = nsigma * np.nanmean(dI[mask][-10:])

            # Mask all points below nsigma * RMS.
            mask = interp1d(r, I, fill_value='extrapolate')(rsky) >= mask
            mask = np.ones(clipped_data.shape) * mask[None, :, :]
            clipped_data = np.where(mask, clipped_data, 0.0)

            # Mask all points below <Tb> - nsigma * d<Tb>.
            r, Tb, dT = self.radial_profile(collapse='max', beam_spacing=False)
            clip = interp1d(r, Tb - nsigma * dT, fill_value='extrapolate')
            clipped_data = np.where(self.data >= clip(rsky), clipped_data, 0.0)

        # Calculate the emission surface and bin appropriately.
        r, z, Tb = self._get_emission_surface(clipped_data, self.x0, self.y0,
                                              self.inc, r_max=1.41*rbins[-1])
        idxs = np.argsort(r)
        r, z, Tb = r[idxs], z[idxs], Tb[idxs]

        if method.lower() not in ['gp', 'binned', 'raw']:
            raise ValueError("method must be 'gp', 'binned' or None.")

        if method.lower() == 'gp':
            window = self.bmaj / np.nanmean(np.diff(r))
            dz = functions.running_stdev(z, window=window)
            r, z, dz = functions.Matern32_model(r, z, dz, jitter=True,
                                                return_var=True)
            z = interp1d(r, z, fill_value=np.nan, bounds_error=False)(rvals)
            dz = interp1d(r, dz, fill_value=np.nan, bounds_error=False)(rvals)

        elif method.lower() == 'binned':
            ridxs = np.digitize(r, rbins)
            dz = [np.nanstd(z[ridxs == rr]) for rr in range(1, rbins.size)]
            z = [np.nanmean(z[ridxs == rr]) for rr in range(1, rbins.size)]
            z, dz = np.squeeze(z), np.squeeze(dz)

        else:
            dz = functions.running_stdev(z, window=window)
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
        ax.set_ylabel(r'Height (arcsec)')
        ax.set_xlabel(r'Radius (arcsec)')
        functions.plotscale(self.bmaj, dx=0.1, dy=0.9, ax=ax)
        return ax

    def _get_emission_surface(self, data, x0, y0, inc, r_max=None):
        """Find the emission surface [r, z, dz] values."""

        coords = []
        tilt = []
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

                # Measure the tilt of the emission surface (north / south).
                tilt += [np.sign(yc - y0)]

        # Use the sign to tell if the closest surface is 'north' or 'south'.
        self.nearest = 'south' if np.sign(np.nanmean(tilt)) > 0 else 'north'
        self.tilt = 1.0 if self.nearest == 'north' else -1.0
        if self.verbose:
            print("Found the %s side is the closest." % self.nearest)
        return np.squeeze(coords).T