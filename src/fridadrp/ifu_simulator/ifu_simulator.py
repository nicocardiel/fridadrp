#
# Copyright 2024 Universidad Complutense de Madrid
#
# This file is part of FRIDA DRP
#
# SPDX-License-Identifier: GPL-3.0-or-later
# License-Filename: LICENSE.txt
#

from astropy.io import fits
from astropy.units import Quantity
import astropy.units as u
from joblib import Parallel, delayed
import json
import numpy as np
import os
import pprint
from scipy.signal import convolve2d
import time
import yaml

from fridadrp.processing.define_3d_wcs import get_wvparam_from_wcs3d
from numina.tools.ctext import ctext

from .display_skycalc import display_skycalc
from .generate_image2d_method0_ifu import generate_image2d_method0_ifu
from .generate_geometry_for_scene_block import generate_geometry_for_scene_block
from .generate_image3d_method0_ifu import generate_image3d_method0_ifu
from .generate_spectrum_for_scene_block import generate_spectrum_for_scene_blok
from .load_atmosphere_transmission_curve import load_atmosphere_transmission_curve
from .raise_valueerror import raise_ValueError
from .save_image2d_detector_method0 import save_image2d_detector_method0
from .save_image2d_rss import save_image2d_rss
from .set_wavelength_unit_and_range import set_wavelength_unit_and_range
from .update_image2d_rss_detector_method0 import update_image2d_rss_detector_method0
from .update_image2d_rss_method1 import update_image2d_rss_method1


pp = pprint.PrettyPrinter(indent=1, sort_dicts=False)


def ifu_simulator(wcs3d, naxis1_detector, naxis2_detector, nslices,
                  noversampling_whitelight,
                  scene_fname,
                  seeing_fwhm_arcsec, seeing_psf,
                  flatpix2pix,
                  atmosphere_transmission,
                  rnoise,
                  faux_dict, rng,
                  prefix_intermediate_fits,
                  verbose=False, instname=None, subtitle=None, plots=False):
    """IFU simulator.

    Parameters
    ----------
    wcs3d : `~astropy.wcs.wcs.WCS`
        WCS of the data cube.
    naxis1_detector : `~astropy.units.Quantity`
        Detector NAXIS1, dispersion direction.
    naxis2_detector : `~astropy.units.Quantity`
        Detector NAXIS2, spatial direction (slices).
    nslices : int
        Number of IFU slices.
    noversampling_whitelight : int
        Oversampling factor (integer number) to generate the white
        light image.
    scene_fname : str
        YAML scene file name.
    seeing_fwhm_arcsec : `~astropy.units.Quantity`
        Seeing FWHM (arcsec).
    seeing_psf : str
        Seeing PSF.
    flatpix2pix : str
        String indicating whether a pixel-to-pixel flatfield is
        applied or not. Two possible values:
        - 'default': use default flatfield defined in 'faux_dict'
        - 'none': do not apply flatfield
    atmosphere_transmission : str
        String indicating whether the atmosphere transmission of
        the atmosphere is applied or not. Two possible values are:
        - 'default': use default curve defined in 'faux_dict'
        - 'none': do not apply atmosphere transmission
    rnoise : `~astropy.units.Quantity`
        Readout noise standard deviation (in ADU). Assumed to be
        Gaussian.
    faux_dict : Python dictionary
        File names of auxiliary files:
        - skycalc: table with SKYCALC Sky Model Calculator predictions
        - flatpix2pix: pixel-to-pixel flat field
        - model_ifu2detector: 2D polynomial transformation
          x_ifu, y_ify, wavelength -> x_detector, y_detector
    rng : `~numpy.random._generator.Generator`
        Random number generator.
    prefix_intermediate_fits : str
        Prefix for output intermediate FITS files. If the length of
        this string is 0, no output is generated.
    verbose : bool
        If True, display additional information.
    instname : str or None
        Instrument name.
    subtitle : str or None
        Plot subtitle.
    plots : bool
        If True, plot intermediate results.

    Returns
    -------
    """

    if verbose:
        print(' ')
        for item in faux_dict:
            print(ctext(f'- Required file for item {item}:\n  {faux_dict[item]}', faint=True))

    if plots:
        # display SKYCALC predictions for sky radiance and transmission
        display_skycalc(faux_skycalc=faux_dict['skycalc'])

    # spatial IFU limits
    naxis1_ifu = Quantity(value=wcs3d.array_shape[2], unit=u.pix, dtype=int)
    naxis2_ifu = Quantity(value=wcs3d.array_shape[1], unit=u.pix, dtype=int)
    min_x_ifu = 0.5 * u.pix
    max_x_ifu = naxis1_ifu + 0.5 * u.pix
    min_y_ifu = 0.5 * u.pix
    max_y_ifu = naxis2_ifu + 0.5 * u.pix

    # wavelength limits
    wv_cunit1, wv_crpix1, wv_crval1, wv_cdelt1 = get_wvparam_from_wcs3d(wcs3d)
    wmin = wv_crval1 + (0.5 * u.pix - wv_crpix1) * wv_cdelt1
    wmax = wv_crval1 + (naxis1_detector + 0.5 * u.pix - wv_crpix1) * wv_cdelt1

    # load atmosphere transmission curve
    wave_transmission, curve_transmission = load_atmosphere_transmission_curve(
        atmosphere_transmission=atmosphere_transmission,
        wmin=wmin,
        wmax=wmax,
        wv_cunit1=wv_cunit1,
        faux_dict=faux_dict,
        verbose=verbose
    )

    required_keys_in_scene_block = {
        'scene_block_name',
        'spectrum',
        'geometry',
        'nphotons',
        'apply_atmosphere_transmission',
        'apply_seeing',
        'render'
    }

    nphotons_all = 0
    simulated_wave_all = None
    simulated_x_ifu_all = None
    simulated_y_ifu_all = None

    # main loop (rendering of scene blocks)
    with open(scene_fname, 'rt') as fstream:
        scene_dict = yaml.safe_load_all(fstream)
        for scene_block in scene_dict:
            scene_block_keys = set(scene_block.keys())
            if scene_block_keys != required_keys_in_scene_block:
                print(ctext(f'ERROR while processing: {scene_fname}', fg='red'))
                print(ctext('expected keys..: ', fg='blue') + f'{required_keys_in_scene_block}')
                print(ctext('keys found.....: ', fg='blue') + f'{scene_block_keys}')
                list_unexpected_keys = list(scene_block_keys.difference(required_keys_in_scene_block))
                if len(list_unexpected_keys) > 0:
                    print(ctext('unexpected keys: ', fg='red') + f'{list_unexpected_keys}')
                list_missing_keys = list(required_keys_in_scene_block.difference(scene_block_keys))
                if len(list_missing_keys) > 0:
                    print(ctext('missing keys...: ', fg='red') + f'{list_missing_keys}')
                pp.pprint(scene_block)
                raise_ValueError(f'Invalid format in file: {scene_fname}')
            scene_block_name = scene_block['scene_block_name']
            if verbose:
                print(ctext(f'\n* Processing: {scene_block_name}', fg='green'))
                pp.pprint(scene_block)
            else:
                print(ctext(f'* Processing: {scene_block_name}', fg='green'))

            nphotons = int(float(scene_block['nphotons']))
            apply_atmosphere_transmission = scene_block['apply_atmosphere_transmission']
            if atmosphere_transmission == "none" and apply_atmosphere_transmission:
                print(ctext(f'WARNING: {apply_atmosphere_transmission=} when {atmosphere_transmission=}', fg='cyan'))
                print(f'{atmosphere_transmission=} overrides {apply_atmosphere_transmission=}')
                print(f'The atmosphere transmission will not be applied!')
                apply_atmosphere_transmission = False
            apply_seeing = scene_block['apply_seeing']
            if apply_seeing:
                if seeing_fwhm_arcsec.value < 0:
                    raise_ValueError(f'Unexpected {seeing_fwhm_arcsec=}')
                elif seeing_fwhm_arcsec == 0:
                    print(ctext(f'WARNING: {apply_seeing=} when {seeing_fwhm_arcsec=}', fg='cyan'))
                    print('Seeing effect will not be applied!')
                    apply_seeing = False
            render = scene_block['render']
            if nphotons > 0 and render:
                # set wavelength unit and range
                wave_unit, wave_min, wave_max = set_wavelength_unit_and_range(
                    scene_fname=scene_fname,
                    scene_block=scene_block,
                    wmin=wmin,
                    wmax=wmax,
                    verbose=verbose
                )
                # generate spectrum
                simulated_wave = generate_spectrum_for_scene_blok(
                    scene_fname=scene_fname,
                    scene_block=scene_block,
                    faux_dict=faux_dict,
                    wave_unit=wave_unit,
                    wave_min=wave_min,
                    wave_max=wave_max,
                    nphotons=nphotons,
                    apply_atmosphere_transmission=apply_atmosphere_transmission,
                    wave_transmission=wave_transmission,
                    curve_transmission=curve_transmission,
                    rng=rng,
                    naxis1_detector=naxis1_detector,
                    verbose=verbose,
                    plots=plots
                )
                # convert to default wavelength_unit
                simulated_wave = simulated_wave.to(wv_cunit1)
                # distribute photons in the IFU focal plane
                simulated_x_ifu, simulated_y_ifu = generate_geometry_for_scene_block(
                    scene_fname=scene_fname,
                    scene_block=scene_block,
                    nphotons=nphotons,
                    apply_seeing=apply_seeing,
                    seeing_fwhm_arcsec=seeing_fwhm_arcsec,
                    seeing_psf=seeing_psf,
                    wcs3d=wcs3d,
                    min_x_ifu=min_x_ifu,
                    max_x_ifu=max_x_ifu,
                    min_y_ifu=min_y_ifu,
                    max_y_ifu=max_y_ifu,
                    rng=rng,
                    verbose=verbose,
                    plots=plots
                )
                # store all simulated photons
                if nphotons_all == 0:
                    simulated_wave_all = simulated_wave
                    simulated_x_ifu_all = simulated_x_ifu
                    simulated_y_ifu_all = simulated_y_ifu
                else:
                    simulated_wave_all = np.concatenate((simulated_wave_all, simulated_wave))
                    simulated_x_ifu_all = np.concatenate((simulated_x_ifu_all, simulated_x_ifu))
                    simulated_y_ifu_all = np.concatenate((simulated_y_ifu_all, simulated_y_ifu))
                # ---
                # update nphotons
                if verbose:
                    print(ctext(f'--> {nphotons} photons simulated', fg='blue'))
                if nphotons_all == 0:
                    nphotons_all = nphotons
                else:
                    nphotons_all += nphotons
                if len({nphotons_all,
                        len(simulated_wave_all),
                        len(simulated_x_ifu_all),
                        len(simulated_y_ifu_all)
                        }) != 1:
                    print(ctext('ERROR: check the following numbers:', fg='red'))
                    print(f'{nphotons_all=}')
                    print(f'{len(simulated_wave_all)=}')
                    print(f'{len(simulated_x_ifu_all)=}')
                    print(f'{len(simulated_y_ifu_all)=}')
                    raise_ValueError('Unexpected differences found in the previous numbers')
            else:
                if verbose:
                    if nphotons == 0:
                        print(ctext('WARNING -> nphotons: 0', fg='cyan'))
                    else:
                        print(ctext('WARNING -> render: False', fg='cyan'))

    # filter simulated photons to keep only those that fall within
    # the IFU field of view and within the expected spectral range
    # (note that this step also removes simulated photons with
    # negative wavelength value corresponding to those absorbed by
    # the atmosphere when applying the transmission curve)
    textwidth_nphotons_number = len(str(nphotons_all))
    if verbose:
        print('\nFiltering photons within IFU field of view and spectral range...')
        print(f'Initial number of simulated photons: {nphotons_all:>{textwidth_nphotons_number}}')
    cond1 = simulated_x_ifu_all >= min_x_ifu
    cond2 = simulated_x_ifu_all <= max_x_ifu
    cond3 = simulated_y_ifu_all >= min_y_ifu
    cond4 = simulated_y_ifu_all <= max_y_ifu
    cond5 = simulated_wave_all >= wmin
    cond6 = simulated_wave_all <= wmax
    iok = np.where(cond1 & cond2 & cond3 & cond4 & cond5 & cond6)[0]

    if len(iok) == 0:
        print(ctext(f'Final number of simulated photons..: {len(iok):>{textwidth_nphotons_number}}', fg='red'))
        raise SystemExit

    if len(iok) < nphotons_all:
        simulated_x_ifu_all = simulated_x_ifu_all[iok]
        simulated_y_ifu_all = simulated_y_ifu_all[iok]
        simulated_wave_all = simulated_wave_all[iok]
        nphotons_all = len(iok)
    if verbose:
        print(ctext(f'Final number of simulated photons..: {nphotons_all:>{textwidth_nphotons_number}}', fg='blue'))

    # ---------------------------------------------------------------
    # compute image2d IFU, white image, with and without oversampling
    # ---------------------------------------------------------------
    if verbose:
        print(ctext('\n* Computing image2d IFU (method 0) with and without oversampling', fg='green'))
    for noversampling in [noversampling_whitelight, 1]:
        generate_image2d_method0_ifu(
            wcs3d=wcs3d,
            noversampling_whitelight=noversampling,
            simulated_x_ifu_all=simulated_x_ifu_all,
            simulated_y_ifu_all=simulated_y_ifu_all,
            prefix_intermediate_fits=prefix_intermediate_fits,
            instname=instname,
            subtitle=subtitle,
            scene=scene_fname,
            plots=plots
        )

    # ----------------------------
    # compute image3d IFU, method0
    # ----------------------------
    if verbose:
        print(ctext('\n* Computing image3d IFU (method 0)', fg='green'))
    bins_x_ifu = (0.5 + np.arange(naxis1_ifu.value + 1)) * u.pix
    bins_y_ifu = (0.5 + np.arange(naxis2_ifu.value + 1)) * u.pix
    bins_wave = wv_crval1 + \
                ((np.arange(naxis2_detector.value + 1) + 1) * u.pix - wv_crpix1) * wv_cdelt1 - 0.5 * u.pix * wv_cdelt1
    generate_image3d_method0_ifu(
        wcs3d=wcs3d,
        simulated_x_ifu_all=simulated_x_ifu_all,
        simulated_y_ifu_all=simulated_y_ifu_all,
        simulated_wave_all=simulated_wave_all,
        bins_x_ifu=bins_x_ifu,
        bins_y_ifu=bins_y_ifu,
        bins_wave=bins_wave,
        prefix_intermediate_fits=prefix_intermediate_fits
    )

    # --------------------------------------------
    # compute image2d RSS and in detector, method0
    # --------------------------------------------
    if verbose:
        print(ctext('\n* Computing image2d RSS and detector (method 0)', fg='green'))
    bins_x_detector = np.linspace(start=0.5, stop=naxis1_detector.value + 0.5, num=naxis1_detector.value + 1)
    bins_y_detector = np.linspace(start=0.5, stop=naxis2_detector.value + 0.5, num=naxis2_detector.value + 1)

    # read ifu2detector transformations
    dict_ifu2detector = json.loads(open(faux_dict['model_ifu2detector'], mode='rt').read())

    # additional degradation in the spectral direction
    # (in units of detector pixels)
    extra_degradation_spectral_direction = rng.normal(loc=0.0, scale=1, size=nphotons_all) * u.pix

    # initialize images
    image2d_rss_method0 = np.zeros((naxis1_ifu.value * nslices, naxis1_detector.value))
    image2d_detector_method0 = np.zeros((naxis2_detector.value, naxis1_detector.value))

    # update images
    # (accelerate computation using joblib.Parallel)
    t0 = time.time()
    """
    for islice in range(nslices):
        print(f'{islice=}')
        update_image2d_rss_detector_method0(...)
    """
    Parallel(n_jobs=-1, prefer="threads")(
        delayed(update_image2d_rss_detector_method0)(
            islice=islice,
            simulated_x_ifu_all=simulated_x_ifu_all,
            simulated_y_ifu_all=simulated_y_ifu_all,
            simulated_wave_all=simulated_wave_all,
            naxis1_ifu=naxis1_ifu,
            bins_x_ifu=bins_x_ifu,
            bins_wave=bins_wave,
            bins_x_detector=bins_x_detector,
            bins_y_detector=bins_y_detector,
            wv_cdelt1=wv_cdelt1,
            extra_degradation_spectral_direction=extra_degradation_spectral_direction,
            dict_ifu2detector=dict_ifu2detector,
            image2d_rss_method0=image2d_rss_method0,
            image2d_detector_method0=image2d_detector_method0
        ) for islice in range(nslices))
    t1 = time.time()
    if verbose:
        print(f'Delta time: {t1 - t0}')

    # save RSS image (note that the flatfield effect is not included!)
    save_image2d_rss(
        wcs3d=wcs3d,
        image2d_rss=image2d_rss_method0,
        method=0,
        prefix_intermediate_fits=prefix_intermediate_fits
    )

    # apply flatpix2pix to detector image
    if flatpix2pix not in ['default', 'none']:
        raise_ValueError(f'Invalid {flatpix2pix=}')
    if flatpix2pix == 'default':
        infile = faux_dict['flatpix2pix']
        with fits.open(infile) as hdul:
            image2d_flatpix2pix = hdul[0].data
        if np.min(image2d_flatpix2pix) <= 0:
            print(f'- minimum flatpix2pix value: {np.min(image2d_flatpix2pix)}')
            raise_ValueError(f'Unexpected signal in flatpix2pix <= 0')
        naxis2_flatpix2pix, naxis1_flatpix2pix = image2d_flatpix2pix.shape
        naxis1_flatpix2pix *= u.pix
        naxis2_flatpix2pix *= u.pix
        if (naxis1_flatpix2pix != naxis1_detector) or (naxis2_flatpix2pix != naxis2_detector):
            raise_ValueError(f'Unexpected flatpix2pix shape: naxis1={naxis1_flatpix2pix}, naxis2={naxis2_flatpix2pix}')
        if verbose:
            print(f'Applying flatpix2pix: {os.path.basename(infile)} to detector image')
            print(f'- minimum flatpix2pix value: {np.min(image2d_flatpix2pix):.6f}')
            print(f'- maximum flatpix2pix value: {np.max(image2d_flatpix2pix):.6f}')
        image2d_detector_method0 /= image2d_flatpix2pix
    else:
        if verbose:
            print('Skipping applying flatpix2pix')

    # apply Gaussian readout noise to detector image
    if rnoise.value > 0:
        if verbose:
            print(f'Applying Gaussian {rnoise=} to detector image')
        ntot_pixels = naxis1_detector.value * naxis2_detector.value
        image2d_rnoise_flatten = rng.normal(loc=0.0, scale=rnoise.value, size=ntot_pixels)
        image2d_detector_method0 += image2d_rnoise_flatten.reshape((naxis2_detector.value, naxis1_detector.value))
    else:
        if verbose:
            print('Skipping adding Gaussian readout noise')

    save_image2d_detector_method0(
        image2d_detector_method0=image2d_detector_method0,
        prefix_intermediate_fits=prefix_intermediate_fits
    )

    # ---------------------------------------------------
    # compute image2d RSS from image in detector, method1
    # ---------------------------------------------------
    if verbose:
        print(ctext('\n* Computing image2d RSS (method 1)', fg='green'))

    # initialize image
    image2d_rss_method1 = np.zeros((naxis1_ifu.value * nslices, naxis1_detector.value))

    if verbose:
        print('Rectifying...')
    t0 = time.time()
    """
    # loop in slices
    for islice in range(nslices):
        update_image2d_rss_method1(
            islice=islice,
            image2d_detector_method0=image2d_detector_method0,
            dict_ifu2detector=dict_ifu2detector,
            naxis1_detector=naxis1_detector,
            naxis1_ifu=naxis1_ifu,
            wv_crpix1=wv_crpix1,
            wv_crval1=wv_crval1,
            wv_cdelt1=wv_cdelt1,
            image2d_rss_method1=image2d_rss_method1,
            debug=False
        )
    """
    Parallel(n_jobs=-1, prefer="threads")(
        delayed(update_image2d_rss_method1)(
            islice=islice,
            image2d_detector_method0=image2d_detector_method0,
            dict_ifu2detector=dict_ifu2detector,
            naxis1_detector=naxis1_detector,
            naxis1_ifu=naxis1_ifu,
            wv_crpix1=wv_crpix1,
            wv_crval1=wv_crval1,
            wv_cdelt1=wv_cdelt1,
            image2d_rss_method1=image2d_rss_method1,
            debug=False
        ) for islice in range(nslices))
    t1 = time.time()
    if verbose:
        print(f'Delta time: {t1 - t0}')

    save_image2d_rss(
        wcs3d=wcs3d,
        image2d_rss=image2d_rss_method1,
        method=1,
        prefix_intermediate_fits=prefix_intermediate_fits
    )

    # ------------------------------------
    # compute image3d IFU from RSS method1
    # ------------------------------------
    if verbose:
        print(ctext('\n* Computing image3d IFU from image2d RSS method 1', fg='green'))

    # kernel in the spectral direction
    # (bidimensional to be applied to a bidimensional image)
    kernel = np.array([[0.25, 0.50, 0.25]])

    # convolve RSS image
    convolved_data = convolve2d(image2d_rss_method1, kernel, boundary='fill', fillvalue=0, mode='same')

    # ToDo: the second dimension in the following array should be 2*nslices
    # (check what to do for another IFU, like TARSIS)
    image3d_ifu_method1 = np.zeros((naxis1_detector.value, naxis2_ifu.value, naxis1_ifu.value))
    if verbose:
        print(f'(debug): {image3d_ifu_method1.shape=}')

    for islice in range(nslices):
        i1 = islice * 2
        j1 = islice * naxis1_ifu.value
        j2 = j1 + naxis1_ifu.value
        image3d_ifu_method1[:, i1, :] = convolved_data[j1:j2, :].T
        image3d_ifu_method1[:, i1+1, :] = convolved_data[j1:j2, :].T

    image3d_ifu_method1 /= 2
    if verbose:
        print(f'(debug): {np.sum(image2d_rss_method1)=}')
        print(f'(debug):      {np.sum(convolved_data)=}')
        print(f'(debug): {np.sum(image3d_ifu_method1)=}')

    # save FITS file
    if len(prefix_intermediate_fits) > 0:
        hdu = fits.PrimaryHDU(image3d_ifu_method1.astype(np.float32))
        hdu.header.extend(wcs3d.to_header(), update=True)
        hdul = fits.HDUList([hdu])
        outfile = f'{prefix_intermediate_fits}_ifu_3D_method1.fits'
        print(f'Saving file: {outfile}')
        hdul.writeto(f'{outfile}', overwrite='yes')
