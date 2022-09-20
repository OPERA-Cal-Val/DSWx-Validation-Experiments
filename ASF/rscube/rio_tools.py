from rasterio.warp import (calculate_default_transform,
                           reproject,
                           Resampling,
                           aligned_target)
from rasterio.transform import xy, rowcol
from affine import Affine
from rasterio import features
from rasterio.features import shapes
import numpy as np
import math
import fiona
from typing import Union, Tuple, List


def get_indices_from_extent(transform: Affine,
                            extent: List[float],
                            shape: tuple = None,
                            res_buffer: int = 0) -> Tuple[tuple]:
    """Obtain Upper left corner and bottom right corner from extents based on
    geo-transform that specifies resolution and upper left corner of a coordinate
    system
    Parameters
    ----------
    transform : Affine
        Affine geo transform
    extent : List[float]
        (xmin, ymin, xmax, ymax) in the CRS of transform
    shape : tuple, optional
        Will bound the indices by (height, width), by default None
    res_buffer : int, optional
        Additional resolution buffer, by default 0
    Returns
    -------
    Tuple[tuple]
        (Coordinates of upper left corner, Coordinates of bottom right corner) where
        coordinates are (row, col) coordinates
    Notes
    -----
    Can use to slice geo-reference arrays based on extents
    """
    xmin, ymin, xmax, ymax = extent
    row_ul, col_ll = rowcol(transform, xmin, ymax, op=math.floor)
    row_br, col_br = rowcol(transform, xmax, ymin, op=math.ceil)

    corner_ul = (max(row_ul - res_buffer, 0),
                 max(col_ll - res_buffer, 0))

    height, width = (np.inf, np.inf)
    if shape is not None:
        height, width = shape

    corner_br = (min(row_br + res_buffer, height),
                 min(col_br + res_buffer, width))

    return corner_ul, corner_br


def get_geopandas_features_from_array(arr: np.ndarray,
                                      transform: Affine,
                                      label_name: str = 'label',
                                      mask: np.ndarray = None,
                                      connectivity: int = 4) -> list:
    """
    Obtains a list of geopandas features in which contigious integers are
    grouped as polygons for use as:

        df =  gpd.GeoDataFrame.from_features(geo_features)

    Parameters
    ----------
    arr : np.ndarray
        The array of integers to group into contiguous polygons. Note some
        labels that are connected through diagonals May be separated depending
        on connectivity.
    transform : Affine
        Rasterio transform related to arr
    label_name : str
        The label name used for each different polygonal feature, default is
        `label`.
    mask : np.ndarray
        Nodata mask in which true values indicate where nodata is located.
    connectivity : int
        4- or 8- connectivity of the polygonal features.  See rasterio:
        https://rasterio.readthedocs.io/en/latest/api/rasterio.features.html#rasterio.features.shapes
        And see: https://en.wikipedia.org/wiki/Pixel_connectivity

    Returns
    -------
    list:
        List of features to use for constructing geopandas dataframe with
        gpd.GeoDataFrame.from_features
    """
    # see rasterio.features.shapes - needs all false values to be no data areas
    if mask is None:
        mask = np.zeros(arr.shape, dtype=bool)
    feature_list = list(shapes(arr,
                               mask=~mask,
                               transform=transform,
                               connectivity=connectivity))
    geo_features = list({'properties': {label_name: value},
                         'geometry': geometry}
                        for i, (geometry, value) in enumerate(feature_list))
    return geo_features


def polygonize_array_to_shapefile(arr: np.ndarray,
                                  profile: dict,
                                  shape_file_dir: str,
                                  label_name: str = 'label',
                                  mask: np.ndarray = None,
                                  connectivity: int = 4):
    """
    Directly create a polygonal shapefile from an array of integers grouping
    pixels with the same value together into a polygon. Polygons with
    contiguous value in array will have attribute determined by `label_name`
    and value determined by arr.

    Parameters
    ----------
    arr : np.ndarray
        The integer array.
    profile : dict
        Rasterio profile corresponding to arr.
    shape_file_dir : str
        The string of the path to saved shapefile. Assumes parent directories
        exist.
    label_name : str
        The attribute name used in the shapefile.
    mask : np.ndarray
        Removes polygons associated with a nodata mask. True values are where
        the nodata are located in arr.
    connectivity : int
        4- or 8- connectivity of the polygonal features.  See rasterio:
        https://rasterio.readthedocs.io/en/latest/api/rasterio.features.html#rasterio.features.shapes
        And see: https://en.wikipedia.org/wiki/Pixel_connectivity
    """
    dtype = str(arr.dtype)
    if 'int' in dtype or 'bool' in dtype:
        arr = arr.astype('int32')
        dtype = 'int32'
        dtype_for_shape_file = 'int'

    if 'float' in dtype:
        arr = arr.astype('float32')
        dtype = 'float32'
        dtype_for_shape_file = 'float'

    crs = profile['crs']
    results = get_geopandas_features_from_array(arr,
                                                profile['transform'],
                                                label_name='label',
                                                mask=mask,
                                                connectivity=connectivity)
    with fiona.open(shape_file_dir, 'w',
                    driver='ESRI Shapefile',
                    crs=crs,
                    schema={'properties': [(label_name, dtype_for_shape_file)],
                            'geometry': 'Polygon'}) as dst:
        dst.writerecords(results)


def rasterize_shapes_to_array(shapes: list,
                              attributes: list,
                              profile: dict,
                              all_touched: bool = False,
                              dtype: str = np.float32) -> np.ndarray:
    """
    Takes a list of geometries and attributes to create an array. Roughly the
    inverse, in spirit, to `get_geopandas_features_from_array`.  For example,
    `shapes = df.geometry` and `attributes = df.label`, where df is a geopandas
    GeoDataFrame. We note the array is initialized as array of zeros.

    Parameters
    ----------
    shapes : list
        List of Shapely geometries.
    attributes : list
        List of attributes corresponding to shapes.
    profile : dict
        Rasterio profile in which shapes will be projected into, importantly
        the transform and dimensions specified.
    all_touched : bool
        Whether factionally covered pixels are written with specific value or
        ignored. See `rasterio.features.rasterize`.
    dtype : str
        The initial array is np.zeros and dtype can be specified as a numpy
        dtype or appropriate string.

    Returns
    -------
    np.ndarray:
        The output array determined with profile.
    """
    out_arr = np.zeros((profile['height'], profile['width']), dtype=dtype)

    # this is where we create a generator of geom, value pairs to use in
    # rasterizing
    shapes = [(geom, value) for geom, value in zip(shapes, attributes)]
    burned = features.rasterize(shapes=shapes,
                                out=out_arr,
                                transform=profile['transform'],
                                all_touched=all_touched)

    return burned


def reproject_arr_to_match_profile(src_array: np.ndarray,
                                   src_profile: dict,
                                   ref_profile: dict,
                                   nodata: str = None,
                                   resampling='bilinear') \
                                           -> Tuple[np.ndarray, dict]:
    """
    Reprojects an array to match a reference profile providing the reprojected
    array and the new profile.  Simply a wrapper for rasterio.warp.reproject.

    Parameters
    ----------
    src_array : np.ndarray
        The source array to be reprojected.
    src_profile : dict
        The source profile of the `src_array`
    ref_profile : dict
        The profile that to reproject into.
    nodata : str
        The nodata value to be used in output profile. If None, the nodata from
        src_profile is used in the output profile.  See
        https://github.com/mapbox/rasterio/blob/master/rasterio/dtypes.py#L13-L24.
    resampling : str
        The type of resampling to use. See all the options:
        https://github.com/mapbox/rasterio/blob/08d6634212ab131ca2a2691054108d81caa86a09/rasterio/enums.py#L28-L40

    Returns
    -------
    Tuple[np.ndarray, dict]:
        Reprojected Arr, Reprojected Profile

    Notes
    -----
    src_array needs to be in gdal (i.e. BIP) format that is (# of channels) x
    (vertical dim.) x (horizontal dim).  Also, works with arrays of the form
    (vertical dim.) x (horizontal dim), but output will be: 1 x (vertical dim.)
    x (horizontal dim).
    """
    height, width = ref_profile['height'], ref_profile['width']
    crs = ref_profile['crs']
    transform = ref_profile['transform']

    reproject_profile = ref_profile.copy()

    nodata = nodata or src_profile['nodata']
    src_dtype = src_profile['dtype']
    count = src_profile['count']

    reproject_profile.update({'dtype': src_dtype,
                              'nodata': nodata,
                              'count': count})

    dst_array = np.zeros((count, height, width))

    resampling = Resampling[resampling]

    reproject(src_array,
              dst_array,
              src_transform=src_profile['transform'],
              src_crs=src_profile['crs'],
              dst_transform=transform,
              dst_crs=crs,
              dst_nodata=nodata,
              resampling=resampling)
    return dst_array.astype(src_dtype), reproject_profile


def get_cropped_profile(profile: dict,
                        slice_x: slice,
                        slice_y: slice) -> dict:
    """
    This is a tool for using a reference profile and numpy slices (i.e.
    np.s_[start: stop]) to create a new profile that is within the window of
    slice_x, slice_y.

    Parameters
    ----------
    profile : dict
        The reference rasterio profile.
    slice_x : slice
        The horizontal slice.
    slice_y : slice
        The vertical slice.

    Returns
    -------
    dict:
        The rasterio dictionary from cropping.
    """

    x_start = slice_x.start or 0
    y_start = slice_y.start or 0
    x_stop = slice_x.stop or profile['width']
    y_stop = slice_y.stop or profile['height']

    if (x_start < 0) | (x_stop < 0) | (y_start < 0) | (y_stop < 0):
        raise ValueError('Slices must be positive')

    width = x_stop - x_start
    height = y_stop - y_start

    profile_cropped = profile.copy()

    trans = profile['transform']
    x_cropped, y_cropped = xy(trans, y_start, x_start, offset='ul')
    trans_list = list(trans.to_gdal())
    trans_list[0] = x_cropped
    trans_list[3] = y_cropped
    tranform_cropped = Affine.from_gdal(*trans_list)
    profile_cropped['transform'] = tranform_cropped

    profile_cropped['height'] = height
    profile_cropped['width'] = width

    return profile_cropped


def get_bounds_dict(profile: dict) -> dict:
    """
    Get the dictionary with bounds in the relevant CRS with keys 'left',
    'right', 'top', 'bottom'.

    Parameters
    ----------
    profile : dict
        The rasterio reference profile

    Returns
    -------
    dict:
        The bounds dictionary.
    """
    lx, ly = profile['width'], profile['height']
    transform = profile['transform']
    bounds_dict = {'left': transform.c,
                   'right': transform.c + transform.a * lx,
                   'top': transform.f,
                   'bottom': transform.f + transform.e * ly
                   }
    return bounds_dict


def reproject_profile_to_new_crs(src_profile: dict,
                                 dst_crs: str,
                                 target_resolution: Union[float, int] = None)\
                                         -> dict:
    """
    Create a new profile into a new CRS based on a dst_crs. May specify
    resolution.

    Parameters
    ----------
    src_profile : dict
        Source rasterio profile.
    dst_crs : str
        Destination CRS, as specified by rasterio.
    target_resolution : Union[float, int]
        Target resolution

    Returns
    -------
    dict:
        Rasterio profile of new CRS
    """
    reprojected_profile = src_profile.copy()
    bounds_dict = get_bounds_dict(src_profile)

    src_crs = src_profile['crs']
    w, h = src_profile['width'], src_profile['height']
    dst_trans, dst_w, dst_h = calculate_default_transform(src_crs,
                                                          dst_crs,
                                                          w, h,
                                                          **bounds_dict
                                                          )

    if target_resolution is not None:
        tr = target_resolution
        dst_trans, dst_w, dst_h = aligned_target(dst_trans,
                                                     dst_w,
                                                     dst_h,
                                                     tr)
    reprojected_profile.update({
                                'crs': dst_crs,
                                'transform': dst_trans,
                                'width': dst_w,
                                'height': dst_h,
                                })
    return reprojected_profile


def reproject_arr_to_new_crs(src_array: np.ndarray,
                             src_profile: dict,
                             dst_crs: str,
                             resampling: str = 'bilinear',
                             target_resolution: float = None) -> \
                                     Tuple[np.ndarray, dict]:
    """
    Reproject an array into a new CRS.

    Parameters
    ----------
    src_array : np.ndarray
        Source array
    src_profile : dict
        Source rasterio profile corresponding to `src_array`
    dst_crs : str
        The destination rasterio CRS to reproject into
    resampling : str
        How to do resampling.  See all the options:
        https://github.com/mapbox/rasterio/blob/08d6634212ab131ca2a2691054108d81caa86a09/rasterio/enums.py#L28-L40
    target_resolution : float
        Target resolution

    Returns
    -------
    Tuple[np.ndarray, dict]:
        (reprojected_array, reprojected_profile) of data.
    """
    tr = target_resolution
    reprojected_profile = reproject_profile_to_new_crs(src_profile,
                                                       dst_crs,
                                                       target_resolution=tr)
    resampling = Resampling[resampling]
    dst_array = np.zeros((reprojected_profile['count'],
                          reprojected_profile['height'],
                          reprojected_profile['width']))

    reproject(
              # Source parameters
              source=src_array,
              src_crs=src_profile['crs'],
              src_transform=src_profile['transform'],
              # Destination paramaters
              destination=dst_array,
              dst_transform=reprojected_profile['transform'],
              dst_crs=reprojected_profile['crs'],
              dst_nodata=src_profile['nodata'],
              # Configuration
              resampling=resampling,
              )
    return dst_array, reprojected_profile


def convert_4326_to_utm(lon: float, lat: float) -> str:
    """
    Obtain UTM zone from (lon, lat) coordinate.

    From: https://gis.stackexchange.com/a/269552

    Parameters
    ----------
    lon : float
        Longitude
    lat : float
        Latitude

    Returns
    -------
    str:
        epsg code, in the form `epsg:<epsg_num>`.
    """
    utm_band = str(int((np.floor((lon + 180) / 6) % 60) + 1))
    if len(utm_band) == 1:
        utm_band = '0'+utm_band
    if lat >= 0:
        epsg_code = '326' + utm_band
    else:
        epsg_code = '327' + utm_band
    return f'epsg:{epsg_code}'
