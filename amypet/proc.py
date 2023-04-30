'''
Processing of PET images for AmyPET 
'''

__author__ = "Pawel Markiewicz"
__copyright__ = "Copyright 2022-3"

import logging as log
import os, shutil
from pathlib import Path, PurePath

import numpy as np
from niftypet import nimpa
import spm12

from .utils import get_atlas
from .dyn_tools import timing_dyn

log.basicConfig(level=log.WARNING, format=nimpa.LOG_FORMAT)



# ========================================================================================
def atl2pet(frefpet, fatl, cldct, outpath=None):
    ''' 
    Atlas and GM from the centiloid (CL) pipeline to the reference
    PET space.
    Arguments:
    - frefpet:  the file path of the reference PET image 
    - fatl:     the file path of the atlas in MNI space
    - cldct:    the CL output dictionary
    '''

    # > output path
    if outpath is None:
        opth = Path(cl_dct['opth']).parent.parent
    else:
        opth = Path(outpath)
    nimpa.create_dir(opth)


    # > decipher the CL dictionary
    if len(cldct)==1:
        cl_dct = cldct[next(iter(cldct))]
    elif 'norm' in cldct:
        cl_dct = cldct
    else:
        raise ValueError('unrecognised CL dictionary')

    # > read the PET image
    petdct = nimpa.getnii(frefpet, output='all')

    # > SPM bounding box of the PET image
    bbox = spm12.get_bbox(petdct)


    # # > get the affine PET->MR
    # if isinstance(affine, (Path, PurePath)) or isinstance(affine, str):
    #     aff = np.loadtxt(affine)
    # elif isinstance(affine, np.array):
    #     aff = np.array(affine)

    # > get the inverse affine transform to PET native space
    M = np.linalg.inv(cl_dct['reg2']['affine'])
    import matlab as ml
    Mm = ml.double(M.tolist())

    # > copy the inverse definitions to be modified with affine to native PET space
    fmod = shutil.copyfile(cl_dct['norm']['invdef'],
                           opth/(Path(cl_dct['norm']['invdef']).name.split('.')[0] + '_2nat.nii'))
    eng = spm12.ensure_spm('')
    eng.amypad_coreg_modify_affine(str(fmod), Mm)

    # > unzip the atlas and transform it to PET space
    fniiatl = nimpa.nii_ugzip(fatl, outpath=opth)

    # > inverse transform the atlas to PET space
    finvatl = spm12.normw_spm(str(fmod), [fniiatl + ',1'], voxsz=petdct['voxsize'], intrp=0., bbox=bbox,
                              outpath=str(opth))[0]

    # > remove the uncompressed input atlas after transforming it
    os.remove(fniiatl)

    # > GM mask
    fgmpet = spm12.resample_spm(frefpet, cl_dct['norm']['c1'], M, intrp=1.0, outpath=opth,
                                pickname='flo', fcomment='_GM_in_PET', del_ref_uncmpr=True,
                                del_flo_uncmpr=True, del_out_uncmpr=True)


    # > remove NaNs
    atl = nimpa.getnii(finvatl)
    atl[np.isnan(atl)] = 0
    gm = nimpa.getnii(fgmpet)
    gm[np.isnan(gm)] = 0


    return dict(fatlpet=finvatl, fgmpet=fgmpet, atlpet=atl, gmpet=gm, outpath=opth)




# ========================================================================================
def extract_vois(impet, atlas, voi_dct, atlas_mask=None, outpath=None, output_masks=False):
    '''
    Extract VOI mean values from PET image `impet` using image labels `atals`.
    Both can be dictionaries, file paths or Numpy arrays.
    They have to be aligned and have the same dimensions.
    If path (output) is given, the ROI masks will be saved to file(s).
    Arguments:
        - impet:    PET image as Numpy array
        - atlas:  image of labels (integer values); the labels can come
                    from T1w-based parcellation or an atlas.
        - voi_dct:  dictionary of VOIs, with entries of labels creating
                    composite volumes
        - atlas_mask: masks the atlas with an additional maks, e.g., with the
                    grey matter probability mask.
        - output_masks: if `True`, output Numpy VOI masks in the output
                    dictionary
        - outpath:  if given as a folder path, the VOI masks will be saved
    '''

    # > assume none of the below are given
    # > used only for saving ROI mask to file if requested
    affine, flip, trnsp = None, None, None

    # ----------------------------------------------
    # PET
    if isinstance(impet, dict):
        im = impet['im']
        if 'affine' in impet:
            affine = impet['affine']
        if 'flip' in impet:
            flip = impet['flip']
        if 'transpose' in impet:
            trnsp = impet['transpose']

    elif isinstance(impet, (str, PurePath)) and os.path.isfile(impet):
        imd = nimpa.getnii(impet, output='all')
        im = imd['im']
        flip = imd['flip']
        trnsp = imd['transpose']

    elif isinstance(impet, np.ndarray):
        im = impet
    # ----------------------------------------------

    # ----------------------------------------------
    # LABELS
    if isinstance(atlas, dict):
        lbls = atlas['im']
        if 'affine' in atlas and affine is None:
            affine = atlas['affine']
        if 'flip' in atlas and flip is None:
            flip = atlas['flip']
        if 'transpose' in atlas and trnsp is None:
            trnsp = atlas['transpose']

    elif isinstance(atlas, (str, PurePath)) and os.path.isfile(atlas):
        prd = nimpa.getnii(atlas, output='all')
        lbls = prd['im']
        if affine is None:
            affine = prd['affine']
        if flip is None:
            flip = prd['flip']
        if trnsp is None:
            trnsp = prd['transpose']

    elif isinstance(atlas, np.ndarray):
        lbls = atlas

    # > get rid of NaNs if any in the parcellation/label image
    lbls[np.isnan(lbls)] = 0

    # > atlas mask
    if atlas_mask is not None:
        if isinstance(atlas_mask, (str, PurePath)) and os.path.isfile(atlas_mask):
            amsk = nimpa.getnii(atlas_mask)
        elif isinstance(atlas_mask, np.ndarray):
            amsk = atlas_mask
        else:
            raise ValueError('Incorrectly provided atlas mask')
    else:
        amsk = 1
    # ----------------------------------------------

    # ----------------------------------------------
    # > output dictionary
    out = {}

    log.debug('Extracting volumes of interest (VOIs):')
    for k, voi in enumerate(voi_dct):

        log.info(f'  VOI: {voi}')

        # > ROI mask
        rmsk = np.zeros(lbls.shape, dtype=bool)

        for ri in voi_dct[voi]:
            log.debug(f'   label{ri}')
            rmsk += np.equal(lbls, ri)

        # > apply the mask on mask
        if not isinstance(amsk, np.ndarray) and amsk==1:
            msk2 = rmsk
        else:
            msk2 = rmsk*amsk

        if outpath is not None and not isinstance(atlas, np.ndarray):
            nimpa.create_dir(outpath)
            fvoi = Path(outpath) / (str(voi) + '_mask.nii.gz')
            nimpa.array2nii(msk2, affine, fvoi,
                            trnsp=(trnsp.index(0), trnsp.index(1), trnsp.index(2)), flip=flip)
        else:
            fvoi = None
        
        vxsum = np.sum(msk2)

        if im.ndim==4:
            nfrm = im.shape[0]
            emsum = np.zeros(nfrm, dtype=np.float64)
            for fi in range(nfrm):
                emsum[fi] = np.sum(im[fi,...].astype(np.float64) * msk2)
        
        elif im.ndims==3:
            emsum = np.sum(im.astype(np.float64)*msk2)
        
        else:
            raise ValueError('unrecognised image shape or dimensions')

        
        out[voi] = {'vox_no': vxsum, 'sum': emsum, 'avg': emsum / vxsum, 'fvoi': fvoi}

        if output_masks:
            out[voi]['roimsk'] = msk2

    # ----------------------------------------------

    return out





# ========================================================================================
def proc_vois(
    niidat,
    aligned_suvr,
    aligned_brk,
    cl_dct,
    atlas='hammers',
    voi_idx=None,
    res=1,
    outpath=None,
    apply_gmmask=True):

    '''
    Process and prepare the VOI dynamic data for kinetic analysis.
    Arguments:
    niidat:     dictionary with NIfTI file paths and properties with time.
    aligned_suvr: dictionary of aligned static frames with the SUVr
                and properties.
    aligned_brk:dictionary of aligned early frames, with properties
    cl_dct:     dictionary of centiloid (CL) processing outputs - used
                for inverse transformation to native image spaces.
    atlas:      choice of atlas; default is the Hammers atlas (atlas='hammers'');
                AAL also is supported (atlas='aal'); any other custom atlas
                can be used if atlas is a path to the NIfTI file of the atlas;
                for custom atlas `voi_idx` must be provided as a dictionary.
    voi_ids:    VOI indices for composite VOIs.  Every atlas has its own
                labelling strategy.
    res:        resolution of the atlas - the default is 1 mm voxel size
                isotropically.
    apply_gmmask: applies the GM mask based on the T1w image to refine
                the VOI sampling.
    '''

    # > output path
    if outpath is None:
        opth = niidat['outpath'].parent/'DYN'
    else:
        opth = outpath
    nimpa.create_dir(opth)


    # > get the atlas
    if isinstance(atlas, (str, PurePath)) and Path(atlas).name.endswith(('.nii', '.nii.gz')):
        fatl = atlas
    elif isinstance(atlas, str) and atlas in ['hammers', 'aal']:
        datl = get_atlas(atlas=atlas, res=res)
        fatl = datl['fatlas']

    if voi_idx is not None and isinstance(voi_idx, dict):
        dvoi = voi_idx
    else:
        if atlas=='all':
            # > New AAL3 codes!
            dvoi=dict(
                    cerebellum=list(range(95,120)),
                    frontal=list(range(1,25))+[73,74],
                    parietal=list(range(61,72)),
                    occipital=list(range(47,59)),
                    temporal=[59,60]+list(range(83,95)),
                    insula=[33,34],
                    precuneus=[71,72],
                    antmidcingulate=list(range(151,157))+[37,38],
                    postcingulate=[39,40],
                    hippocampus=[41,42],
                    caudate=[75,76],
                    putamen=[77,78],
                    thalamus=list(range(121,151)),
                    composite=list(range(3,29))+list(range(31,37))+list(range(59,69))+list(range(63,72))+list(range(85,91))
                    )
        elif atlas=='hammers':
            dvoi=dict(
                    cerebellum=[17,18],
                    frontal=[28,29]+list(range(50,60))+list(range(68,74))+list(range(76,82)),
                    parietal=[32,33, 60,61,62,63, 84,85],
                    occipital=[22,23, 64,65,66,67],
                    temporal=list(range(5,17))+[82,83],
                    insula=[20,21]+list(range(86,96)),
                    antecingulate=[24,25],
                    postcingulate=[26,27],
                    hippocampus=[1,2],
                    caudate=[34,35],
                    putamen=[38,39],
                    thalamus=[40,41],
                    composite=[28,29]+list(range(52,60))+list(range(76,82))+list(range(86,96))+[32,33, 62,63, 84,85],
                    )
        else:
            raise ValueError('unrecognised atlas name!')


    # > get the atlas and GM probability mask in PET space using CL inverse pipeline
    atlgm = atl2pet(aligned_suvr['suvr']['fsuvr'], fatl, cl_dct, outpath=opth)

    # > TO DO: the dynamic image can be in aligned_suvr

    if apply_gmmask:
        gmmsk = atlgm['fgmpet']
    else:
        gmmsk = None
    
    rvoi = extract_vois(aligned_brk['fpet'], atlgm['fatlpet'], dvoi, atlas_mask=gmmsk, outpath=opth/'masks', output_masks=True)


    # > timing of all frames
    tdct = timing_dyn(niidat)

    # > frame time definitions for NiftyPAD 
    dt = tdct['niftypad']

    return dict(dt=dt, voi=rvoi, atlas_gm=atlgm, outpath=opth)
