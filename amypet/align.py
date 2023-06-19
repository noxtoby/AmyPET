'''
Aligning tools for PET dynamic frames
'''

__author__ = "Pawel Markiewicz"
__copyright__ = "Copyright 2022-3"

import logging as log
import os
import shutil
from datetime import datetime
from pathlib import Path
from subprocess import run
import copy

from itertools import combinations
import dcm2niix
import numpy as np
import spm12
from miutil.imio import nii
from miutil import hasext
from niftypet import nimpa



from .utils import get_atlas
from .suvr_tools import preproc_suvr
from .preproc import id_acq

# > tracer in different radionuclide group
f18group = ['fbb', 'fbp', 'flute']
c11group = ['pib']

# > list of registration/motion metrics for alignment
reg_metric_list = ['rss', 'adst']

log.basicConfig(level=log.WARNING, format=nimpa.LOG_FORMAT)


#--------------------------------------------------------------------
def save4dnii(lfrm, fnii, descrip='AmyPET generated', retarr=True):
    '''
    Save 4D NIfTI image from a list of files of consecutive 
    frames `lfrm` to file path `fnii`.
    '''

    # > number of frames for the whole study
    nfrm4d = len(lfrm)
    tmp = nimpa.getnii(lfrm[0], output='all')
    niia = np.zeros((nfrm4d,)+tmp['shape'], dtype=np.float32)
    for i, frm in enumerate(lfrm):
        im_ = nimpa.getnii(frm)
        # > remove NaNs if any
        im_[np.isnan(im_)] = 0
        niia[i, ...] = im_

    # > save aligned frames
    nimpa.array2nii(
        niia,
        tmp['affine'],
        fnii,
        descrip=descrip,
        trnsp=(tmp['transpose'].index(0),
               tmp['transpose'].index(1),
               tmp['transpose'].index(2)),
        flip=tmp['flip'])

    if retarr:
        return niia
    else:
        return None


#--------------------------------------------------------------------
def align_frames(
    fniis,
    times,
    fref,
    Cnt,
    reg_tool='spm',
    save4d=True,
    f4d=None,
    outpath=None):
    '''
    Align frames by mashing the short ones as defined by the threshold.
    Arguments:
    fniis:      list of PET frames for alignment
    times:      is a list or an array of start/stop (2D list/array) or
                a 1D time duration list/array.
    fref:       the NIfTI file of a reference frame to which registration/
                alignment is performed.
    reg_tool:   the software used for registration/alignment, by default
                it is SPM ('spm'), alternatively it can be DIPY ('dipy').
    saved4d:    if True, saves aligned frames into a one 4D NIfTI file.
    f4d:        the file name of the 4D output NIfTI file.
    reg_fwhm:   FWHM of the smoothing used for reference and floating 
                images in registration.
    reg_costfun:the registration cost function, by default the normalised
                mutual information, 'nmi'.

    '''


    # > the FWHM of the Gaussian kernel used for smoothing the images before registration and only for registration purposes.
    reg_fwhm = Cnt['align']['reg_fwhm']
    # > the threshold above which registration transformation is deemed to represent motion (not noise); based on a metric
    # > of combined rotations and translations or average distance (rigid body).
    reg_thrshld = Cnt['align']['reg_thrshld']
    reg_costfun = Cnt['align']['reg_costfun']
    # > the shortest PET frame to be used for registration in the alignment process.
    frm_lsize = Cnt['align']['frame_min_dur']
    reg_metric = Cnt['align']['reg_metric']


    if reg_metric not in reg_metric_list:
        raise ValueError('Unrecognised registration metric.')


    if reg_tool=='dipy':
        if reg_metric=='rss':
            log.warning('`rss` is not supported option for DIPY registration - switching to `adst`')
            reg_metric='adst'


    # > number of frames
    nfrm = len(fniis)

    if isinstance(times, list) or isinstance(times, np.ndarray):
        tmp = np.array(times)
        if tmp.ndim==2:
            ts = tmp
            dur = np.zeros(nfrm)
            dur = np.array([t[1]-t[0] for t in ts])
        else:
            raise ValueError('unrecognised dimension for frame times')

        if len(dur)!=len(fniis):
            raise ValueError('the number of frames must be the same as number of frame time definitions')
    else:
        raise ValueError('unrecognised frame times')


    #----------------------------------
    # > output dictionary
    outdct = {}

    if outpath is None:
        opth = Path(fniis[0]).parent
    else:
        opth = Path(outpath)

    mniidir = opth/'mashed'
    rsmpl_opth = mniidir/'aligned'
    nimpa.create_dir(mniidir)
    nimpa.create_dir(rsmpl_opth)
    #----------------------------------
    

    # > short frame size for registration (in seconds)
    frms_l = dur<frm_lsize

    # > number of frames to be mashed for registration
    nmfrm = np.sum(frms_l)

    # > number of resulting mashed frame sets, e.g., the consecutive frames can be mashed into one or more frames
    nset = int(np.floor(np.sum(dur[frms_l])/frm_lsize))

    # > overall list of mashed frames and normal frames which are longer than `frm_lsize`
    mfrms = []

    nmfrm_chck = 0
    # > mashing frames for registration
    for i in range(nset):
        sfrms = ts[:,1]<=(i+1)*frm_lsize
        sfrms *= ts[:,1]> i*frm_lsize

        # > list of keys of frames to be mashed
        ifrm = [i for i in range(nfrm) if sfrms[i]]

        # > append to the overall list of mashed frames
        mfrms.append(ifrm)

        # > update the overall number of frames to be mashed
        nmfrm_chck += len(ifrm)


    # >> CHECKS >>
    #---------------------
    if nmfrm_chck!=nmfrm:
        raise ValueError('Mashing frames inconsistent: number of frames to be mashed incorrectly established.')
    #---------------------
    # > add the normal length (not mashed) frames
    for i,frm in enumerate(~frms_l):
        if frm:
            mfrms.append([i])
            nmfrm_chck += 1
    #---------------------
    if nmfrm_chck!=nfrm:
        raise ValueError('Mashing frames inconsistency: number of overall frames, including mashed, is incorrectly established.')
    #---------------------
    # << <<


    # > the output file paths of mashed frames
    mfrms_out = []

    # > generate NIfTI series of mashed frames for registration
    for mgrp in mfrms:

        # > image holder
        tmp = nimpa.getnii(fniis[mgrp[0]], output='all')
        im = np.zeros(tmp['shape'], dtype=np.float32)
        
        for i in mgrp:
            im += nimpa.getnii(fniis[i])

        # > output file path and name
        fout = mniidir/(f'mashed_n{len(mgrp)}_' + fniis[mgrp[0]].name)

        # > append the mashed frames output file path
        mfrms_out.append(fout)

        nimpa.array2nii(
            im,
            tmp['affine'],
            fout,
            descrip='mashed PET frames for registration',
            trnsp=tmp['transpose'],
            flip=tmp['flip'])

    if len(mfrms_out)!=len(mfrms):
        raise ValueError('The number of generated mashed frames is inconsistent with the intended mashed frames')


    outdct['mashed_frame_idx'] = mfrms
    outdct['mashed_files'] = mfrms_out

    # > initialise the array for metric of registration result (sum of angles+translations)
    R = np.zeros(len(mfrms_out))

    # > similar metric but uses a sampled average distance imposed by the affine transformation
    D = np.zeros(len(mfrms_out))

    # > full rotations and translations for mashed and full frames
    RT = np.zeros((len(mfrms_out), 6), dtype=np.float32)
    RTf= np.zeros((nfrm, 6), )

    # > affine file outputs
    S = [None for _ in range(len(mfrms_out))]

    # > aligned/resampled file names
    faligned = [None for _ in range(nfrm)]

    # > counter for base frames
    fi = 0

    # > register the mashed frames to the reference (SUVr frame by default)
    for mi, mfrm in enumerate(mfrms_out):

        # # > make sure the images are not compressed, i.e., ending with .nii
        # if not mfrm.name.endswith('.nii'):
        #     raise ValueError('The mashed frame files should be uncompressed NIfTI')

        # > pick the reference point as the centre of mass of the floating imaging (used in `D`)
        _, com_ = nimpa.centre_mass_rel(mfrm)

        if reg_tool=='spm':
            # > register mashed frames to the reference
            spm_res = nimpa.coreg_spm(fref, mfrm, fwhm_ref=reg_fwhm, fwhm_flo=reg_fwhm,
                                      fwhm=[13, 13], costfun=reg_costfun,
                                      fcomment=f'_mashed_ref-mfrm', outpath=mniidir,
                                      visual=0, save_arr=False, del_uncmpr=True, pickname='flo')

            S[mi] = (spm_res['faff'])

            RT[mi,:] = np.concatenate((spm_res['rotations'], spm_res['translations']), axis=0)
            rot_ss = np.sum((180 * spm_res['rotations'] / np.pi)**2)**.5
            trn_ss = np.sum(spm_res['translations']**2)**.5
            R[mi] = rot_ss + trn_ss

            # > average distance due to transformation/motion
            D[mi] = nimpa.aff_dist(spm_res['affine'], com_)

        elif reg_tool=='dipy':
            # > register mashed frames to the reference using DIPY
            dipy_res = nimpa.affine_dipy(
                fref,
                mfrm,
                metric='MI',
                outpath=mniidir,
                fcomment=f'_mashed_ref-mfrm',
                rfwhm=reg_fwhm,
                ffwhm=reg_fwhm)

            S[mi] = (dipy_res['faff'])

            # > average distance due to transformation/motion
            D[mi] = nimpa.aff_dist(dipy_res['affine'], com_)
            

        # > align each frame through resampling 
        for i in mfrms[mi]:

            # > get the rotation and translation parameters for each frame
            RTf[i,:] = RT[mi,:]

            if (R[mi]>reg_thrshld) * (reg_metric=='rss') or (D[mi]>reg_thrshld) * (reg_metric=='adst'):
                # > resample images for alignment

                if reg_tool=='spm':
                    faligned[fi] = nimpa.resample_spm(
                        fref,
                        fniis[i],
                        S[mi],
                        intrp=1.,
                        outpath=rsmpl_opth,
                        pickname='flo',
                        del_ref_uncmpr=True,
                        del_flo_uncmpr=True,
                        del_out_uncmpr=True,
                    )
                elif reg_tool=='dipy':

                    rsmpld = nimpa.resample_dipy(
                        fref,
                        fniis[i],
                        faff=S[mi],
                        outpath=rsmpl_opth,
                        pickname='flo',
                        intrp=1)

                    faligned[fi] = rsmpld['fnii']

            else:
                faligned[fi] = rsmpl_opth/fniis[i].name
                shutil.copyfile(fniis[i], faligned[fi])

            fi += 1


    outdct['affines'] = S
    outdct['metric'] = R
    outdct['metric2'] = D
    # > for every frame:
    outdct['rot_trans'] = RTf
    outdct['faligned'] = faligned

    if save4d:
        if f4d is not None and Path(f4d).parent.is_dir():
            falign_nii = f4d
        else:
            falign_nii = opth/'aligned_via_mashing_using_ref_4D.nii.gz'

        niia = save4dnii(faligned, falign_nii, descrip='AmyPET-aligned frames', retarr=True)

        outdct['f4d'] = falign_nii
        outdct['im4d'] = niia

    return outdct


# =====================================================================
def align(niidat,
          Cnt,
          reg_tool='spm',
          com_correction=True,
          outpath=None,
          use_stored=True):

    ''' align all the frames in static, dynamic or coffee-break
        acquisitions.
    '''

    if outpath is None and 'outpath' not in niidat:
        k = niidat['descr'][0]['frms'][0]
        opth = niidat['series'][0][k]['fnii'].parent
    elif outpath is not None:
        opth = Path(outpath)
        nimpa.create_dir(opth)
    elif 'outpath' in niidat:
        opth = Path(niidat['outpath'])


    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # IDENTIFY SUVR/STATIC SERIES DATA
    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    stat_tdata = id_acq(niidat, acq_type='suvr')
    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # ALIGN PET FRAMES FOR STATIC/DYNAMIC IMAGING
    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # > align the PET frames around the equilibrium static scan (SUVr)
    aligned_suvr = align_suvr(
        stat_tdata,
        Cnt,
        reg_tool=reg_tool,
        com_correction=com_correction,
        outpath=opth,
        use_stored=use_stored)


    # > align for all dynamic frames (if any remaining)
    aligned_dyn = align_break(
        niidat,
        aligned_suvr,
        Cnt,
        reg_tool=reg_tool,
        use_stored=use_stored)
    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    return aligned_dyn



# =====================================================================
def align_suvr(
    stat_tdata,
    Cnt,
    reg_tool='spm',
    outpath=None,
    save_not_aligned=True,
    use_stored=False,
    com_correction=True,
    save_params=False,
):
    '''
    Align SUVr frames after conversion to NIfTI format.

    Arguments:
    - com_correction: centre-of-mass correction - moves the coordinate system to the 
                centre of the spatial image intensity distribution.
    - save_params:  save all the rotations and translations into a 3D matrix
    - reg_tool:     the registration tool/method; SPM by default ('spm'), DIPY as
                    an alternative ('dipy')
    - reg_metric:   metric used in evaluating the amount motion when deciding
                    motion correction. reg_tool='adst' does average sampled distance;
                    the other option is the summed root sum square of
                    translations and rotations, 'rss', available only for SPM.

    '''

    # > the FWHM of the Gaussian kernel used for smoothing the images before registration and only for registration purposes.
    reg_fwhm = Cnt['align']['reg_fwhm']
    # > the metric and threshold for the registration/motion when deciding to apply the transformation
    reg_metric  = Cnt['align']['reg_metric']
    reg_thrshld = Cnt['align']['reg_thrshld']
    reg_costfun = Cnt['align']['reg_costfun']
    

    if reg_metric not in reg_metric_list:
        raise ValueError('Unrecognised registration metric.')


    if reg_tool=='dipy':
        if reg_metric=='rss':
            log.warning('`rss` is not supported option for DIPY registration - switching to `adst`')
            reg_metric='adst'


    if outpath is None:
        align_out = stat_tdata[stat_tdata['descr']['frms'][0]]['fnii'].parent.parent
    else:
        align_out = Path(outpath)


    # > number of PET frames in series with static/SUVr data
    nfrm = len(stat_tdata['descr']['frms'])

    # > Nnumber of frames for uptake ratio image (SUVr)
    nsfrm = len(stat_tdata['descr']['suvr']['frms'])


    # > NIfTI output folder
    niidir = align_out / 'NIfTI_aligned'
    niidir_i = niidir / 'intermediate'
    nimpa.create_dir(niidir)
    nimpa.create_dir(niidir_i)

    # > folder of resampled and aligned NIfTI files (SPM)
    rsmpl_opth = niidir / 'SPM-aligned'
    nimpa.create_dir(rsmpl_opth)

    tmp = stat_tdata[stat_tdata['descr']['frms'][0]]
    if 'tstudy' in tmp:
        t_ = 'study-'+tmp['tstudy']
    elif 'tacq' in tmp:
        t_ = 'acq-'+tmp['tacq']
    else:
        t_ = ''

    # > re-aligned output file names and output dictionary
    faligned   = f'SUVr-aligned_{nsfrm}-frames_' + nimpa.rem_chars(stat_tdata[stat_tdata['descr']['frms'][0]]['series']) + '.nii.gz'
    faligned_c = f'SUVr-aligned_{nsfrm}-frames_' + com_correction*('CoM_') + nimpa.rem_chars(stat_tdata[stat_tdata['descr']['frms'][0]]['series']) + '.nii.gz'
    faligned_s = f'Aligned-{nfrm}-frames-to-SUVr_' + nimpa.rem_chars(stat_tdata[stat_tdata['descr']['frms'][0]]['series']) + '.nii.gz'
    falign_dct = f'Aligned-{nfrm}-frames-to-SUVr_{t_}_' + nimpa.rem_chars(stat_tdata[stat_tdata['descr']['frms'][0]]['series'])+'.npy'
    faligned = niidir_i/faligned
    faligned_s = niidir / faligned_s
    faligned_c = niidir_i / faligned_c
    falign_dct = niidir / falign_dct

    # > the same for the not aligned frames, if requested
    fnotaligned = 'SUVr_NOT_aligned_' + nimpa.rem_chars(stat_tdata[stat_tdata['descr']['frms'][0]]['series']) + '.nii.gz'
    fnotaligned = niidir_i / fnotaligned

    # > Matrices: motion metric + paths to affine
    R = S = None

    outdct = None

    # > check if the file exists
    if not use_stored or not falign_dct.is_file():

        # -----------------------------------------------
        # > list all NIfTI files
        nii_frms = []
        for k in stat_tdata['descr']['suvr']['frms']:
            nii_frms.append(stat_tdata[k]['fnii'])
        # -----------------------------------------------

        # -----------------------------------------------
        # > CORE ALIGNMENT OF SUVR FRAMES:

        # > frame-based motion metric (rotations+translation)
        R = np.zeros((len(nii_frms), len(nii_frms)), dtype=np.float32)
        # > average sampled distance alternative metric
        D = np.zeros((len(nii_frms), len(nii_frms)), dtype=np.float32)

        # > paths to the affine files
        S = [[None for _ in range(len(nii_frms))] for _ in range(len(nii_frms))]

        # > go through all possible combinations of frame registration
        for c in combinations(stat_tdata['descr']['suvr']['frms'], 2):
            frm0 = stat_tdata['descr']['suvr']['frms'].index(c[0])
            frm1 = stat_tdata['descr']['suvr']['frms'].index(c[1])

            fnii0 = nii_frms[frm0]
            fnii1 = nii_frms[frm1]

            log.info(f'2-way registration of frame #{frm0} and frame #{frm1}')

            if reg_tool=='spm':
                #....................................................
                # > one way registration (1)
                spm_res = nimpa.coreg_spm(
                                fnii0,
                                fnii1,
                                fwhm_ref=reg_fwhm,
                                fwhm_flo=reg_fwhm,
                                fwhm=[13, 13],
                                costfun=reg_costfun,
                                fcomment=f'_combi_{frm0}-{frm1}',
                                outpath=niidir,
                                visual=0,
                                save_arr=False,
                                del_uncmpr=True)

                rot_ss = np.sum((180 * spm_res['rotations'] / np.pi)**2)**.5
                trn_ss = np.sum(spm_res['translations']**2)**.5
                R[frm0, frm1] = rot_ss + trn_ss

                # > pick the reference point as the centre of mass of the floating imaging (used in `D`)
                _, com_ = nimpa.centre_mass_rel(fnii1)
                # > average distance due to transformation/motion
                D[frm0, frm1] = nimpa.aff_dist(spm_res['affine'], com_)

                S[frm0][frm1] = spm_res['faff']


                # > the other way registration
                spm_res = nimpa.coreg_spm(
                                fnii1,
                                fnii0,
                                fwhm_ref=reg_fwhm,
                                fwhm_flo=reg_fwhm,
                                fwhm=[13, 13],
                                costfun=reg_costfun,
                                fcomment=f'_combi_{frm1}-{frm0}',
                                outpath=niidir,
                                visual=0,
                                save_arr=False,
                                del_uncmpr=True)

                rot_ss = np.sum((180 * spm_res['rotations'] / np.pi)**2)**.5
                trn_ss = np.sum(spm_res['translations']**2)**.5
                R[frm1, frm0] = rot_ss + trn_ss
                
                # > pick the reference point as the centre of mass of the floating imaging (used in `D`)
                _, com_ = nimpa.centre_mass_rel(fnii0)
                # > average distance due to transformation/motion
                D[frm1, frm0] = nimpa.aff_dist(spm_res['affine'], com_)

                S[frm1][frm0] = spm_res['faff']
                #....................................................

            

            elif reg_tool=='dipy':
                #....................................................
                # > 1st way of registration using DIPY
                dipy_res = nimpa.affine_dipy(
                                fnii0,
                                fnii1,
                                rfwhm=reg_fwhm,
                                ffwhm=reg_fwhm,
                                outpath=niidir,
                                fcomment=f'_combi_{frm0}-{frm1}')

                S[frm0][frm1] = (dipy_res['faff'])

                # > average distance due to transformation/motion
                _, com_ = nimpa.centre_mass_rel(fnii1)
                D[frm0, frm1] = nimpa.aff_dist(dipy_res['affine'], com_)


                # > 2nd way of registration using DIPY
                dipy_res = nimpa.affine_dipy(
                                fnii1,
                                fnii0,
                                rfwhm=reg_fwhm,
                                ffwhm=reg_fwhm,
                                outpath=niidir,
                                fcomment=f'_combi_{frm1}-{frm0}')

                S[frm1][frm0] = (dipy_res['faff'])

                # > average distance due to transformation/motion
                _, com_ = nimpa.centre_mass_rel(fnii0)
                D[frm1, frm0] = nimpa.aff_dist(dipy_res['affine'], com_)
                #....................................................



        # > FIND THE REFERENCE FRAME
        # > sum frames along floating frames
        fsum = np.sum(R, axis=0)
        fsumd= np.sum(D, axis=0)

        # > sum frames along reference frames
        rsum = np.sum(R, axis=1)
        rsumd= np.sum(D, axis=1)

        # > reference frame for SUVr composite frame
        rfrm = np.argmin(fsum + rsum)
        rfrmd= np.argmin(fsumd + rsumd)

        print('reference frame using rss', rfrm)
        print('reference frame using adst', rfrmd)

        niiref = nimpa.getnii(nii_frms[rfrm], output='all')

        # > initialise target aligned SUVr image
        niiim = np.zeros((len(nii_frms),) + niiref['shape'], dtype=np.float32)

        # > copy in the target frame for SUVr composite
        niiim[rfrm, ...] = niiref['im']

        # > aligned individual frames, starting with the reference 
        fnii_aligned = [nii_frms[rfrm]]

        for ifrm in range(len(nii_frms)):
            
            # > ignore the reference frame already dealt with
            if ifrm == rfrm:
                continue

            # > check if the motion for this frame is large enough to warren correction
            if (R[rfrm, ifrm]>reg_thrshld) * (reg_metric=='rss') or (D[rfrm, ifrm]>reg_thrshld) * (reg_metric=='adst'):

                # > resample images for alignment
                if reg_tool=='spm':
                    frsmpl = nimpa.resample_spm(
                        nii_frms[rfrm],
                        nii_frms[ifrm],
                        S[rfrm][ifrm],
                        intrp=1.,
                        outpath=rsmpl_opth,
                        pickname='flo',
                        del_ref_uncmpr=True,
                        del_flo_uncmpr=True,
                        del_out_uncmpr=True,
                    )
                
                elif reg_tool=='dipy':
                    rsmpld = nimpa.resample_dipy(
                        nii_frms[rfrm],
                        nii_frms[ifrm],
                        faff=S[rfrm][ifrm],
                        outpath=rsmpl_opth,
                        pickname='flo',
                        intrp=1)

                    frsmpl = rsmpld['fnii']


                fnii_aligned.append(frsmpl)
                niiim[ifrm, ...] = nimpa.getnii(frsmpl)

            else:
                fnii_aligned.append(nii_frms[ifrm])
                niiim[ifrm, ...] = nimpa.getnii(nii_frms[ifrm])
        
        
        # > remove NaNs
        niiim[np.isnan(niiim)] = 0
        
        # > save aligned SUVr frames
        nimpa.array2nii(
            niiim, niiref['affine'], faligned, descrip='AmyPET: aligned SUVr frames',
            trnsp=(niiref['transpose'].index(0), niiref['transpose'].index(1),
                   niiref['transpose'].index(2)), flip=niiref['flip'])


        #+++++++++++++++++++++++++++++++++++++++++++++++++++++
        # SINGLE SUVR FRAME  &  CoM CORRECTION
        #+++++++++++++++++++++++++++++++++++++++++++++++++++++
        # > preprocess the aligned PET into a single SUVr frame
        suvr_frm = preproc_suvr(faligned, outpath=niidir, com_correction=com_correction)
        fref = suvr_frm['fcom']
        #+++++++++++++++++++++++++++++++++++++++++++++++++++++

        # > saved frames aligned and CoM-modified
        fniic_aligned = []
        niiim[:] = 0
        for i in range(len(fnii_aligned)):
            com_ = nimpa.centre_mass_corr(fnii_aligned[i], outpath=rsmpl_opth, com=suvr_frm['com'])
            fniic_aligned.append(com_['fim'])
            niiim[i, ...] = nimpa.getnii(com_['fim'])

        # > save aligned SUVr frames
        tmp = nimpa.getnii(fref, output='all')
        nimpa.array2nii(
            niiim, tmp['affine'], faligned_c, descrip='AmyPET: aligned SUVr frames' + com_correction*(', CoM-modified'),
            trnsp=(tmp['transpose'].index(0), tmp['transpose'].index(1),
                   tmp['transpose'].index(2)), flip=tmp['flip'])
        # -----------------------------------------------

        # > output dictionary
        outdct = dict(suvr={}, wide={})

        outdct['suvr'] = {
            'fpet': faligned_c,
            'fsuvr':fref,
            'fpeti':fniic_aligned,
            'outpath': niidir,
            'Metric': R,
            'faff': S}

        # > save static image which is not aligned
        if save_not_aligned:
            nii_noalign = np.zeros(niiim.shape, dtype=np.float32)
            for k, fnf in enumerate(nii_frms):
                nii_noalign[k, ...] = nimpa.getnii(fnf)

            nimpa.array2nii(
                nii_noalign, niiref['affine'], fnotaligned,
                descrip='AmyPET: unaligned SUVr frames',
                trnsp=(niiref['transpose'].index(0), niiref['transpose'].index(1),
                       niiref['transpose'].index(2)), flip=niiref['flip'])

            outdct['suvr']['fpet_notaligned'] = fnotaligned



        #+++++++++++++++++++++++++++++++++++++++++++++++++++++
        # The remaining frames of static or fully dynamic PET
        #+++++++++++++++++++++++++++++++++++++++++++++++++++++

        # > indices of non-suvr frames
        idx_r = [not f in stat_tdata['descr']['suvr']['frms'] for f in stat_tdata['descr']['frms']]

        falign = np.array([stat_tdata[f]['fnii'] for f in stat_tdata['descr']['frms']])
        ts = np.array(stat_tdata['descr']['timings'])

        aligned_wide = align_frames(
            list(falign[idx_r]),
            ts[idx_r],
            fref,
            Cnt,
            save4d=False, f4d=None,
            outpath=niidir)


        # > output files for affines
        S_w = [None for _ in range(nfrm)]

        # > motion metrics for any remaining frames
        R_w = np.zeros(nfrm)
        D_w = np.zeros(nfrm)

        # > output paths of aligned images for the static part
        fnii_aligned_w = [None for _ in range(nfrm)]

        niiim_ = np.zeros((nfrm,) + niiref['shape'], dtype=np.float32)

        # > index/counter for UR/SUVr frames and wide frames
        fsi = 0
        fwi = 0

        for fi, frm in enumerate(falign):
            if idx_r[fi]:
                S_w[fi] = aligned_wide['affines'][fwi]
                R_w[fi] = aligned_wide['metric'][fwi]
                D_w[fi] = aligned_wide['metric2'][fwi]
                fnii_aligned_w[fi] = aligned_wide['faligned'][fwi]
                niiim_[fi, ...] = nimpa.getnii(fnii_aligned_w[fi])
                fwi += 1
            else:
                # > already aligned as part of SUVr
                fnii_aligned_w[fi] = Path(outdct['suvr']['fpeti'][fsi])
                S_w[fi] = S[rfrm][fsi]
                R_w[fi] = R[rfrm][fsi]
                niiim_[fi, ...] = niiim[fsi,...]
                fsi += 1

        # > save aligned static/dynamic frames
        nimpa.array2nii(
            niiim_, niiref['affine'], faligned_s, descrip='AmyPET: aligned static frames',
            trnsp=(niiref['transpose'].index(0), niiref['transpose'].index(1),
                   niiref['transpose'].index(2)), flip=niiref['flip'])

        outdct['wide'] = {
            'fpet': faligned_s,
            'fpeti':fnii_aligned_w,
            'outpath': niidir,
            'Metric': R_w,
            'faff': S_w}

        np.save(falign_dct, outdct)
        #+++++++++++++++++++++++++++++++++++++++++++++++++++++
    else:
        outdct = np.load(falign_dct, allow_pickle=True)
        outdct = outdct.item()
 
    return outdct
# =====================================================================




# ========================================================================================
def align_break(
    niidat,
    aligned_suvr,
    Cnt,
    reg_tool='spm',
    use_stored=False,
    ):
    
    ''' Align the Coffee-Break protocol data to Static/SUVr data
        to form one consistent dynamic 4D NIfTI image
        Arguments:
        - niidat:   dictionary of all input NIfTI series.
        - aligned_suvr: dictionary of the alignment output for SUVr frames
        - reg_tool: the method of registration used in aligning PET frames,
                    by default it is SPM ('spm') with the alternative of
                    DIPY ('dipy')
        - frame_min_dur: 
        - decay_corr: 
    '''

    reg_fwhm = Cnt['align']['reg_fwhm']
    # > the threshold for the registration metric (combined trans. and rots) when deciding to apply the transformation
    reg_thrshld = Cnt['align']['reg_thrshld']
    # > registration cost function
    reg_costfun = Cnt['align']['reg_costfun']
    # > the shortest PET frame to be used for registration in the alignment process.
    # frame_min_dur=Cnt['align']['frame_min_dur']
    # > correct for decay between different series relative to the earliest one
    decay_corr=Cnt['align']['decay_corr'],

    # > identify coffee-break data if any
    bdyn_tdata = id_acq(niidat, acq_type='break')

    if not bdyn_tdata:
        log.info('no coffee-break protocol data detected.')
        return aligned_suvr

    #-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.
    if decay_corr:
        # > DECAY CORRECTION
        # > get the start time of each series for decay correction if requested
        ts = [sri['time'][0] for sri in niidat['descr']]
        # > index the earliest ref time
        i_tref = np.argmin(ts)
        idxs = list(range(len(ts)))
        idxs.pop(i_tref)
        i_tsrs = idxs
        # > time difference
        td = [ts[i]-ts[i_tref] for i in i_tsrs]
        if len(td)>1:
            raise ValueError('currently only one dynamic break is allowed - detected more than one')
        else:
            td = td[0]

        # > what tracer / radionuclide is used?
        istp = 'F18'*(niidat['tracer'] in f18group) + 'C11'*(niidat['tracer'] in c11group)

        # > decay constant using half-life
        lmbd = np.log(2) / nimpa.resources.riLUT[istp]['thalf']

        # > decay correction factor
        dcycrr = 1/np.exp(-lmbd * td)
    else:
        dcycrr = 1.
    #-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.


    # # > output folder for mashed frames for registration/alignment
    # mniidir = niidat['outpath']/'mashed_break'
    # rsmpl_opth = mniidir/'aligned'
    # nimpa.create_dir(mniidir)
    # nimpa.create_dir(rsmpl_opth)


    tstudy = bdyn_tdata[bdyn_tdata['descr']['frms'][0]]['tstudy']
    
    # > output dictionary
    falign_dct = niidat['outpath']/'NIfTI_aligned'/f'Dynamic-early-frames_study-{tstudy}_aligned-to-SUVr-ref.npy'
    
    
    if use_stored and falign_dct.is_file():
        outdct = np.load(falign_dct, allow_pickle=True)
        return outdct.item()


    # > get the aligned wide/static NIfTI files
    faligned_stat = aligned_suvr['wide']['fpeti']

    # > reference frame (SUVr by default)
    fref = aligned_suvr['suvr']['fsuvr']


    # > dynamic frames to be aligned
    fniis = [bdyn_tdata[k]['fnii'] for k in bdyn_tdata['descr']['frms']]
    # > timings of the frames
    ts = np.array(bdyn_tdata['descr']['timings'])

    #----------------------------------
    aligned = align_frames(
        fniis,
        ts,
        fref,
        Cnt,
        reg_tool=reg_tool,
        save4d=False,
        outpath=niidat['outpath'])
    #----------------------------------


    #+++++++++++++++++++++++++++++++++++++++++++++++++++++++++
    # > number of frames for the whole study
    fall = aligned['faligned']+faligned_stat
    nfrma = len(fall)
    tmp = nimpa.getnii(aligned['faligned'][0], output='all')
    niia = np.zeros((nfrma,)+tmp['shape'], dtype=np.float32)
    for fi, frm in enumerate(aligned['faligned']):
        im_ = nimpa.getnii(frm)
        # > remove NaNs if any
        im_[np.isnan(im_)] = 0
        niia[fi, ...] = im_

    for fii, frm in enumerate(faligned_stat):
        im_ = dcycrr * nimpa.getnii(frm)
        # > remove NaNs if any
        im_[np.isnan(im_)] = 0
        niia[fi+1+fii, ...] = im_

    nfrm = niia.shape[0]
    # > output file
    falign_nii = niidat['outpath']/'NIfTI_aligned'/f'Dynamic-{nfrm}-frames_study-{tstudy}_aligned-to-SUVr-ref.nii.gz'

    # > save aligned frames
    nimpa.array2nii(
        niia,
        tmp['affine'],
        falign_nii,
        descrip='AmyPET: aligned dynamic frames',
        trnsp=(tmp['transpose'].index(0), tmp['transpose'].index(1),
               tmp['transpose'].index(2)),
        flip=tmp['flip'])

    #+++++++++++++++++++++++++++++++++++++++++++++++++++++++++

    outdct = dict(fpet=falign_nii, fpeti=fall, alignment=aligned)
    outdct.update(aligned_suvr)
    np.save(falign_dct, outdct)

    return outdct
# ========================================================================================

