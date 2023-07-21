from skimage.io import imsave
from skimage.filters import threshold_isodata, threshold_otsu
from skimage.segmentation import watershed, clear_border
from skimage.morphology import dilation, disk
from skimage.measure import regionprops
from skimage.measure import label as connected_compos_labeling
from skimage.feature import peak_local_max
from matplotlib.colors import LinearSegmentedColormap
from scipy.ndimage import median_filter, gaussian_laplace, distance_transform_cdt, label
from termcolor import colored
import os, cv2, shutil
import numpy as np
from cellpose import models, utils, io
from datetime import datetime
from skimage import exposure
from scipy.stats import kstest


def create_random_lut():
    """
    Creates a random LUT of 256 slots to display labeled images with colors far apart from each other.
    Black is reserved for the background.

    Returns:
        LinearSegmentedColormap: A cmap object that can be used with the imshow() function. ex: `imshow(image, cmap=create_random_lut())`
    """
    return LinearSegmentedColormap.from_list('random_lut', np.vstack((np.array([(0.0, 0.0, 0.0)]), np.random.uniform(0.01, 1.0, (255, 3)))))


def make_outlines(labeled_cells, thickness=2):
    """
    Turns a labeled image into a mask showing the outline of each label.
    The resulting image is boolean. (==mask)

    Args:
        labeled_cells: The image containing labels.
        thickness: The desired thickness of outlines.
    
    Returns:
        numpy.array: A mask representing outlines of cells.
    """
    dilated = dilation(labeled_cells, disk(thickness)) - labeled_cells
    return dilated > 0


def write_labels_image(image, font_scale):
    regions   = regionprops(image)
    canvas    = np.zeros(image.shape, dtype=np.uint8)
    thickness = 2

    for region in regions:
        y, x = region.centroid
        size, baseline = cv2.getTextSize(str(region.label), cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        cv2.putText(canvas, str(region.label), (int(x-size[0]/2), int(y+size[1]/2)), cv2.FONT_HERSHEY_SIMPLEX, font_scale, 255, thickness)

    return canvas

def find_focused_slice(stack, around=2):
    """
    Determines which slice has the best focus and selects a range of slices around it.
    The process is based on the variance recorded for each slice.
    Displays a warning if the number of slices is insufficient.
    A safety check is performed to ensure that the returned indices are within the range of the stack's size.

    Args:
        stack: (image stack) The stack in which we search the focused area.
        around: (int) Number of slices to select around the most in-focus one.

    Returns:
        (int, int): A tuple centered around the most in-focus slice. If we call 'F' the index of that slice, then the tuple is: `(F-around, F+around)`.
    """
    # If we don't have a stack, we just return a tuple filled with zeros.
    if len(stack.shape) < 3:
        print("The image is a single slice, not a stack.")
        return (0, 0)

    nSlices, width, height = stack.shape
    maxSlice = np.argmax([cv2.Laplacian(stack[s], cv2.CV_64F).var() for s in range(nSlices)])
    selected = (max(0, maxSlice-around), min(nSlices-1, maxSlice+around))
    
    print(f"Selected slices: ({selected[0]+1}, {selected[1]+1}). ", end="")
    print(colored(f"({nSlices} slices available)", 'dark_grey'))
    if selected[1]-selected[0] != 2*around:
        print(colored("Focused slice too far from center!", 'yellow'))

    return selected


def segment_yeasts_cells(transmission, gpu=True):
    """
    Takes the transmission channel (brightfield) of yeast cells and segments it (instances segmentation).
    | CPU: 43s
    | GPU: 11s
    
    Args:
        transmission (image): Single channeled image, in brightfield, representing yeasts
    
    Returns:
        (image) An image containing labels (one value == one individual).
    """
    model = models.Cellpose(gpu=gpu, model_type='cyto')
    chan = [0, 0]
    print("Segmenting cells...")
    masks, flows, styles, diams = model.eval(transmission, diameter=None, channels=chan)
    print("Cells segmentation done.")
    return masks


def contrast_stretching(image, percentile=0.1):
    """
    Contrast stretching by skipping a certain percentile of the histogram before rescaling the values.

    Args:
        image: The original image
        percentile: Percentile of values skipped on each side of the histogram.

    Returns:
        image: An image with contrast enhanced.
    """
    vmin, vmax = np.percentile(image, [percentile, 100 - percentile])
    return exposure.rescale_intensity(image, in_range=(vmin, vmax))


def increase_contrast(image, targetType=np.uint16):
    """
    The resulting image occupies the whole range of possible values according to its data type (uint8, uint16, ...)

    Returns:
        void

    Args:
        image: The image that will beneficiate of a contrast enhancement
    """
    image = image.astype(np.float64)
    image -= image.min()
    image /= image.max()
    image *= np.iinfo(targetType).max
    image = image.astype(targetType)
    
    return image


def place_markers(shp, m_list):
    """
    Places pixels with an incremental intensity (from 1) at each position contained in the list.

    Returns:
        A mask with black background and one pixel at each intensity from 1 to len(m_list).

    Args:
        shp: A 2D tuple representing the shape of the mask to be created.
        m_list: A list of tuples representing 2D coordinates.
    """
    tmp = np.zeros(shp, dtype=np.uint16)
    for i, (l, c) in enumerate(m_list, start=1):
        tmp[l, c] = i
    print(f"{len(m_list)} markers placed.")
    return tmp


#################################################################################


def segment_transmission(stack, gpu=True, slices_around=2):
    """
    Takes the path of an image that contains some yeasts in transmission.

    Args:
        stack: A numpy array representing the transmission channel

    Returns:
        A uint16 image containing labels. Each label corresponds to an instance of yeast cell.
    """
    # Boolean value determining if we want to use all the slices of the stack, or just the most in-focus.
    pick_slices   = True

    # >>> Opening stack as an image collection:
    stack_sz  = stack.shape
    input_bf  = None
    
    if len(stack_sz) > 2: # We have a stack, not a single image.
        # >>> Finding a range of slices in the focus area:
        if pick_slices:
            in_focus = find_focused_slice(stack, slices_around)
        else:
            in_focus = (0, stack.shape[0])

        # >>> Max projection of the stack:
        max_proj = np.max(stack[in_focus[0]:in_focus[1]], axis=0)
        input_bf = max_proj
    else:
        input_bf = np.squeeze(stack)
    
    # >>> Labeling the transmission channel:
    labeled_transmission = segment_yeasts_cells(input_bf, gpu)

    # >>> Finding and removing the labels touching the borders:
    # nCellsBefore = len(np.unique(labeled_transmission))-1
    # cleared_bd   = clear_border(labeled_transmission, buffer_size=3)
    # nCellsAfter  = len(np.unique(cleared_bd))-1
    # print(colored(f"{nCellsBefore-nCellsAfter} cells discarded due to border. {nCellsAfter} cells remaining.", 'yellow'))

    return labeled_transmission, input_bf


#################################################################################


def segment_spots(stack, labeled_cells=None, sigma=3.0, peak_d=5):
    """
    Args:
        stack: A numpy array representing the fluo channel

    Returns:
        A dictionary containing several pieces of information about spots.
         - original: The input image after maximal projection
         - contrasted: A version of the channel stretched on the whole histogram.
         - mask: A labeled image containing an index per detected spot.
         - locations: A list of 2D coordinates representing each spot.
    """

    # >>> Opening fluo spots stack
    stack_sz  = stack.shape
    input_fSpots = None

    # >>> Max projection of the stack
    if len(stack_sz) > 2: # We have a stack, not a single image.
        input_fSpots = np.max(stack, axis=0)
    else:
        input_fSpots = np.squeeze(stack)

    # >>> Contrast augmentation + noise reduction
    print("Starting spots segmentation...")
    save_fSpots  = np.copy(input_fSpots)
    input_fSpots = increase_contrast(input_fSpots)
    input_fSpots = median_filter(input_fSpots, size=3)

    # >>> LoG filter + thresholding
    asf  = input_fSpots.astype(np.float64)
    LoG  = gaussian_laplace(asf, sigma=sigma)
    t    = threshold_isodata(LoG)
    mask = LoG < t

    # >>> Detection of spots location
    asf     = mask.astype(np.float64)
    chamfer = distance_transform_cdt(asf)
    maximas = peak_local_max(chamfer, min_distance=peak_d)

    if labeled_cells is not None:
        clean_points = []
        for l, c in maximas:
            if labeled_cells[l, c] > 0:
                clean_points.append((l, c))
        maximas = np.array(clean_points)
    print(f"{len(maximas)} spots found.")

    # >>> Isolating instances of spots
    m_shape   = mask.shape[0:2]
    markers   = place_markers(m_shape, maximas)
    lbd_spots = watershed(~mask, markers, mask=mask).astype(np.uint16)

    # >>> Returning the results
    return maximas, lbd_spots, save_fSpots


def estimate_uniformity(pts, shape, pvalue=0.02):
    if len(pts) <= 0:
        return True

    points = np.array(pts, dtype=np.float64) / max(shape[0], shape[1])
    x, y = zip(*points)
    xs = kstest(x, 'uniform')
    ys = kstest(y, 'uniform')
    is_unif = (xs.statistic < pvalue) and (ys.statistic < pvalue)

    if is_unif:
        print("The spots are scattered following a uniform distribution.")

    return is_unif


def associate_spots_yeasts(labeled_cells, labeled_spots, fluo_spots, area_threshold, solidity_threshold, extent_threshold, death_threshold):
    """
    Associates each spot with the label it belongs to.
    A safety check is performed to make sure no spot falls in the background.

    Args:
        labeled_cells: A single-channeled image with dtype=uint16 containing the segmented transmission image.
        labeled_spots: A labelised image representing spots
        fluo_spots: The original fluo image containing spots.

    Returns:
        A dictionary in which keys are the labels of each cell. Each key points to a list of dictionary. Each element of the list corresponds to a spot.
        An image representing labels in the fluo channel (a label per spot) is also returned.
    """
    unique_values = np.unique(labeled_cells)
    ownership     = {int(u): [] for u in unique_values if (u > 0)}
    spots_props   = regionprops(labeled_spots, intensity_image=fluo_spots)

    for spot in spots_props:
        cds = [int(k) for k in spot.centroid]
        r, c = cds
        lbl = int(labeled_cells[r, c])

        if lbl == 0:
            continue # We are in the background

        if float(spot['area']) > area_threshold:
            continue
        
        if float(spot['solidity']) < solidity_threshold:
            continue
        
        if float(spot['extent']) < extent_threshold:
            continue
        
        ownership[lbl].append({
            'label'         : int(spot['label']),
            'location'      : (r, c),
            'intensity_mean': round(float(spot['intensity_mean']), 3),
            'intensity_min' : round(float(spot['intensity_min']), 3),
            'intensity_max' : round(float(spot['intensity_max']), 3),
            'area'          : round(float(spot['area']), 3),
            'perimeter'     : round(float(spot['perimeter']), 3),
            'solidity'      : round(float(spot['solidity']), 3),
            'extent'        : round(float(spot['extent']), 3),
            'intensity_sum' : int(np.sum(spot.intensity_image))
        })
    
    # Too many spots correspond to a dead cell.
    ownership = {key: value for key, value in ownership.items() if len(value) < death_threshold}
    
    compare = np.array([item['label'] for sub_list in ownership.values() for item in sub_list])
    removed_mask = np.isin(labeled_spots, compare, invert=True)
    labeled_spots[removed_mask] = 0

    return ownership, np.array([item['location'] for sub_list in ownership.values() for item in sub_list]), labeled_spots


def prepare_directory(path):
    if os.path.exists(path):
        # Empty the folder
        for filename in os.listdir(path):
            file_path = os.path.join(path, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                print(f"Failed to delete {file_path}. Reason: {e}")
    else:
        # Create the folder
        os.makedirs(path)


def segment_nuclei(labeled_yeasts, stack_fluo_nuclei, threshold_coverage, threshold_size_nucleus):
    """
    This function starts by segmenting nuclei.
    Then it curates the results to have exactly one nucleus per cell and remove dying cells.
    
    Args:
        labeled_yeasts: The labeled yeast cells.
        fluo_nuclei: The fluo channel where nuclei are marked.
        threshold_coverage: Value between 0 and 1. If the nucleus covers a cell over this ratio, it is discarded.
        threshold_size_nucleus: Below this size, a nucleus is discarded.
    
    Returns
        The segmentation of cells fixed to have the nuclei of each cell, and the segmented nuclei.
    """
    # >>> Opening fluo spots stack
    stack_sz    = stack_fluo_nuclei.shape
    fluo_nuclei = None

    # >>> Max projection of the stack
    if len(stack_sz) > 2: # We have a stack, not a single image.
        fluo_nuclei = np.max(stack_fluo_nuclei, axis=0)
    else:
        fluo_nuclei = np.squeeze(stack_fluo_nuclei)
    
    # Creating a basic mask representing nuclei.
    t = threshold_otsu(fluo_nuclei)
    mask_nuclei = fluo_nuclei > t
    lbl_nuclei = connected_compos_labeling(mask_nuclei)

    # Dict giving, for each nucleus index, every cell index it participates in.
    # It allows to check which nuclei fall in the background, which ones are in a dividing cell, ...
    participations_cells = {}
    # Dict giving, for each cell index, every nuclei it participates in.
    # This is used to check errors where some cells visually have several nuclei.
    participation_nuclei = {}
    # Dict giving for each tuple (nucleus index, cell index), the number of pixels in the intersection.
    intersection_area = {}

    # For each nuclei, which cells is it involved in?
    for (l, c), idx_n in np.ndenumerate(lbl_nuclei):
        if idx_n == 0:
            continue
        idx_c = labeled_yeasts[l, c]
        participations_cells.setdefault(idx_n, set()).add(idx_c)
        participation_nuclei.setdefault(idx_c, set()).add(idx_n)
        intersection_area.setdefault((idx_c, idx_n), 0)
        intersection_area[(idx_c, idx_n)] += 1

    hist_nuclei, _ = np.histogram(lbl_nuclei, bins=np.max(lbl_nuclei)+1)
    hist_yeasts, _ = np.histogram(labeled_yeasts, bins=np.max(labeled_yeasts)+1)

    # Calculating all the nuclei that must be discarded.
    discarded = set()
    for idx_n, cells in participations_cells.items():
        # If the nuclei is smaller than a threshold in pixels.
        if hist_nuclei[idx_n] < threshold_size_nucleus:
            discarded.add(idx_n)
            continue
        # If the nuclei has more than 10 of its pixels in the background.
        if (0 in cells) and (intersection_area[(0, idx_n)] >= 10):
            discarded.add(idx_n)
            continue
        # If the nuclei participates in more than 2 cells.
        if len(cells) not in {1, 2}:
            discarded.add(idx_n)
            continue

    for idx_c, nuclei in participation_nuclei.items():
        # If a cell is intersected by several nuclei
        if len(nuclei) != 1:
            discarded = discarded.union(set(nuclei))

    # Remove nuclei that cover over 75% of their cell's surface.
    for idx_n, cells in participations_cells.items():
        for idx_c in cells:
            if hist_nuclei[idx_n] / hist_yeasts[idx_c] > threshold_coverage:
                discarded.add(idx_n)

    # Removing labels corresponding to invalid nuclei.
    discarded_arr = np.array([i for i in discarded])
    removed_mask  = np.isin(lbl_nuclei, discarded_arr)
    lbl_nuclei[removed_mask] = 0

    # Removing cells in contact with invalid nuclei.
    mask_nuclei     = lbl_nuclei > 0
    lbl_yeasts_copy = np.copy(labeled_yeasts)
    lbl_yeasts_copy[np.logical_not(mask_nuclei)] = 0
    valid_labels    = np.unique(lbl_yeasts_copy)
    valid_cells     = np.copy(labeled_yeasts)
    removed_mask    = np.isin(valid_cells, valid_labels, invert=True)
    valid_cells[removed_mask] = 0

    # Modifying cells' labels to assemble dividing cells.
    canvas = np.zeros(valid_cells.shape, dtype=np.int64)
    for idx_n, cells in participations_cells.items():
        for idx_c in cells:
            if idx_c > 0:
                canvas += np.where(valid_cells == idx_c, idx_n, 0)
    canvas = canvas.astype(np.uint16)
    return fluo_nuclei, canvas, lbl_nuclei # Fixed segmented yeasts and segmented nuclei.

def distance_spot_nuclei(labeled_cells, labeled_nuclei, labeled_spots, spots_list):
    pass

def create_reference_to(labeled_cells, labeled_spots, spots_list, name, control_dir_path, source_path, projection_cells, projection_spots, indices):
    present = datetime.now()

    # Export projections.
    imsave(
        os.path.join(control_dir_path, name+"_bf.tif"),
        projection_cells)
    imsave(
        os.path.join(control_dir_path, name+"_fluo.tif"),
        contrast_stretching(projection_spots, 0.001))

    # Create outlines of cells, save them along labeled cells.
    outlines = make_outlines(labeled_cells)
    indices  = indices > 0
    outlines = np.logical_or(outlines, indices)
    imsave(
        os.path.join(control_dir_path, name+"_outlines.tif"),
        outlines)
    imsave(
        os.path.join(control_dir_path, name+"_cells.tif"),
        labeled_cells)

    # Create the CSV with spots list, save it along segmented spots
    np.savetxt(
        os.path.join(control_dir_path, name+".csv"),
        spots_list,
        delimiter=',',
        header="axis-0, axis-1")
    imsave(
        os.path.join(control_dir_path, name+"_spots.tif"),
        labeled_spots)

    # Saving the index to read the folder
    f = open(os.path.join(control_dir_path, "index.txt"), 'w')
    f.write("name\n")
    f.write(name+"\n")
    f.write("sources\n")
    f.write(source_path+"\n")
    f.write("time\n")
    f.write(present.strftime("%d/%B/%Y (%H:%M:%S)")+"\n")
    f.close()
