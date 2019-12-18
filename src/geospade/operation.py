import ogr
import cv2
import math
import shapely
import numpy as np
from numba import jit
from copy import deepcopy

from geospade.errors import GeometryUnkown


def rasterise_polygon(points, sres=1., buffer=0.):
    """
    Rasterises a polygon defined by clockwise list of points with the edge-flag algorithm.


    Parameters
    ----------
    points: list of tuples
        Clockwise list of x and y coordinates defining a polygon.
    sres: float, optional
        Spatial resolution of the raster (default is 1).
    inner: bool, optional
        If true, only pixels with their center inside the polygon will be marked as a polygon pixel.

    Returns
    -------
    raster: 2D list
        Binary list where zeros are background pixels and ones are foreground (polygon) pixels.

    Notes
    -----
    The edge-flag algorithm was partly taken from https://de.wikipedia.org/wiki/Rasterung_von_Polygonen
    """

    EPS = 1e-6  # machine epsilon, hard-coded
    buffer = abs(buffer)  # TODO: for the time being only the absolute value is used
    # split tuple points into x and y coordinates (Note: zip does not work with Numba)
    xs, ys = list(zip(*points))
    # define extent of the polygon
    x_min = min(xs)
    x_max = max(xs)
    y_min = min(ys)
    y_max = max(ys)

    # number of columns and rows
    n_rows = round((y_max - y_min)/sres) + 1 + 2*buffer
    n_cols = round((x_max - x_min)/sres) + 1 + 2*buffer
    # raster with zeros
    raster = np.zeros((n_rows, n_cols), np.uint8)

    # first, draw contour of polygon
    for idx in range(1, len(xs)):  # loop over all points of the polygon
        x_1 = xs[idx - 1]
        x_2 = xs[idx]
        y_1 = ys[idx - 1]
        y_2 = ys[idx]
        x_diff = x_2 - x_1
        y_diff = y_2 - y_1
        if y_diff == 0.:  # horizontal line (will be filled later on)
            continue

        k = y_diff / x_diff if x_diff != 0. else None  # slope is None if the line is vertical

        # define start, end and iterator y coordinate and start x coordinate
        if y_1 < y_2:
            y_start = y_1
            y_end = y_2
            x_start = x_1
            y = y_1
        else:
            y_start = y_2
            y_end = y_1
            x_start = x_2
            y = y_2

        while abs(y - y_end - sres) >= EPS:  # iterate along polyline
            if k is not None:
                x = (y - y_start)/k + x_start   # compute x coordinate depending on y coordinate
            else:  # vertical -> x coordinate does not change
                x = x_start

            # compute raster indexes
            i = round(abs((y - y_max)) / sres) + buffer
            j = round(abs((x - x_min)) / sres) + buffer
            raster[i, j] = 1
            y = y + sres  # increase y in steps of 'sres'

    # loop over rows and fill raster from left to right
    for i in range(n_rows):
        is_inner = False
        if sum(raster[i, :]) < 2:  # if there is only one contour point in a line (e.g. spike of a polygon), continue
            continue
        for j in range(n_cols):
            if raster[i, j]:
                is_inner = ~is_inner
            if is_inner:
                raster[i, j] = 1

    if buffer != 0.:
        kernel = np.ones((3, 3), np.uint8)
        raster = cv2.erode(raster, kernel, iterations=buffer)
        raster = raster[buffer:-buffer, buffer:-buffer]

    return raster


@jit(nopython=True)
def get_quadrant(x, y):
    """
    Returns the quadrant in a mathematical positive system:
    1: first quadrant
    2: second quadrant
    3: third quadrant
    4: fourth quadrant
    None: either one or both coordinates are zero

    Parameters
    ----------
    x: float
        x coordinate.
    y: float
        y coordinate.

    Returns
    -------
    int or None
        Quadrant number.
    """

    if x > 0 and y > 0:
        return 1
    elif x < 0 and y > 0:
        return 2
    elif x < 0 and y < 0:
        return 3
    elif x > 0 and y < 0:
        return 4
    else:
        return None


@jit(nopython=True)
def construct_geotransform(origin, rot, px, deg=True):
    """
    A helper function, that constructs the GDAL Geotransform tuple given the
    origin, rotation angle with respect to grid and pixel sizes.

    Parameters
    ----------
    orig: 2-tuple/list
        Coordinates of the lower-left corner, i.e. (x, y).
    rot: float
        Rotation angle in  degrees and radians depending on `deg`.
    px: 2-tuple/list
        Tuple of pixel sizes, i.e. (x_ps, y_ps).
    deg: boolean
        Denotes, whether angle is being parsed in degrees or radians:
            True    => degrees (default)
            False   => radians

    Returns
    -------
    6-tuple
        GDAL geotransform tuple.
    """

    gt = [0, 1, 0, 0, 0, 1]
    gt[0], gt[3] = origin
    alpha = round(math.radians(rot)) if deg else rot
    x_ps, y_ps = px
    gt[1] = math.cos(alpha) * x_ps
    gt[2] = -math.sin(alpha) * x_ps
    gt[4] = math.sin(alpha) * y_ps
    gt[5] = math.cos(alpha) * y_ps
    return tuple(gt)


@jit(nopython=True)
def polar_point(orig, dist, angle):
    """
    Computes a new point by specifying a distance and an azimuth from a given
    known point (Polarpunkt Verfahren).
    :param orig: 2-tuple or 2-list
        tuple or list containing the coordinates of the known point
    :param dist: float
        distance from the known point to the new point
    :param angle: float
        azimuth to the new point in respect to the x-axis (horizontal)
        in radians in interval from -pi to +pi
    :return: tuple
        coordinates of new point
    """
    x, y = orig
    nx = round(x + dist * math.cos(angle),13)
    ny = round(y + dist * math.sin(angle),13)

    return nx, ny

@jit(nopython=True)
def get_inner_angle(polygon, deg=True):
    """


    Parameters
    ----------
    polygon: shapely.geometry or ogr.Geometry
        Clock-wise ordered polygon
    deg: boolean, optional
        Denotes, whether angle is being parsed in degrees or radians:
            True    => degrees (default)
            False   => radians

    Returns
    -------
    float
        Sum of inner angles in degree or radians.
    """

    if isinstance(polygon, ogr.Geometry):
        polygon = shapely.wkt.loads(polygon.ExportToWKT())

    vertices = list(polygon.exterior.coords)
    vertices = vertices.append(vertices[1])
    inner_angles = []
    for i in range(1, len(vertices)-1):
        prev_vertice = np.array(vertices[i-1])
        this_vertice = np.array(vertices[i])
        next_vertice = np.array(vertices[i+1])
        a = prev_vertice - this_vertice
        b = next_vertice - this_vertice
        inner_angle = np.arccos(np.dot(a, b)/(np.linalg.norm(a) * np.linalg.norm(b)))
        inner_angles.append(inner_angle)

    inner_angle_sum = sum(inner_angles)
    if deg:
        inner_angle_sum *= (np.pi/180.)
    return inner_angle_sum

# TODO: check projection when wrapping around date line
def bbox2polygon(bbox, osr_sref=None, segment=None):
    """
    create a polygon geometry from bounding-box bbox, given by
    a set of two points, spanning a polygon area
    bbox : list
        list of coordinates representing the rectangle-region-of-interest
        in the format of [(left, lower), (right, upper)]
    osr_sref : OGRSpatialReference, optional
        spatial reference of the coordinates in bbox
    segment : float
        for precision: distance of longest segment of the geometry polygon
        in units of input osr_sref
    Returns
    -------
    geom_area : OGRGeometry
        a geometry representing the input bbox as
        a) polygon-geometry when defined by a rectangle bbox
        b) point-geometry when defined by bbox through tuples of coordinates
    """

    bbox2 = deepcopy(bbox)

    # wrap around dateline (considering left-lower and right-upper logic).
    if bbox2[0][0] > bbox2[1][0]:
        bbox2[1] = (bbox2[1][0] + 360, bbox2[1][1])

    corners = [(float(bbox2[0][0]), float(bbox2[0][1])),
               (float(bbox2[0][0]), float(bbox2[1][1])),
               (float(bbox2[1][0]), float(bbox2[1][1])),
               (float(bbox2[1][0]), float(bbox2[0][1]))]

    return create_polygon_geometry(corners, osr_sref=osr_sref, segment=segment)


def create_polygon_geometry(points, osr_sref=None, segment=None):
    """
    returns polygon geometry defined by list of points
    Parameters
    ----------
    points : list
        points defining the polygon, either...
        2D: [(x1, y1), (x2, y2), ...]
        3D: [(x1, y1, z1), (x2, y2, z2), ...]
    osr_sref : OGRSpatialReference, optional
        spatial reference to what the geometry should be transformed to
    segment : float, optional
        for precision: distance in units of input osr_sref of longest
        segment of the geometry polygon
    Returns
    -------
    OGRGeometry
        a geometry projected in the target spatial reference
    """
    # create ring from all points
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for p in points:
        if len(p) == 2:
            p += (0.0,)
        ring.AddPoint(*p)
    ring.CloseRings()

    # create the geometry
    polygon_geometry = ogr.Geometry(ogr.wkbPolygon)
    polygon_geometry.AddGeometry(ring)

    # assign spatial reference
    if osr_sref is not None:
        polygon_geometry.AssignSpatialReference(osr_sref)

    # modify the geometry such it has no segment longer then the given distance
    if segment is not None:
        polygon_geometry = segmentize_geometry(polygon_geometry, segment=segment)

    return polygon_geometry


def segmentize_geometry(geometry, segment=0.5):
    """
    segmentizes the lines of a geometry
    Parameters
    ----------
    geometry : OGRGeometry
        geometry object
    segment : float, optional
        for precision: distance in units of input osr_sref of longest
        segment of the geometry polygon
    Returns
    -------
    OGRGeometry
        a congruent geometry realised by more vertices along its shape
    """

    geometry_out = geometry.Clone()

    geometry_out.Segmentize(segment)

    geometry = None
    return geometry_out


def any_geom2ogr_geom(geom, osr_sref=None):
    """
    Transforms an extent represented in different ways or a Shapely geometry object into an OGR geometry object.

    Parameters
    ----------
    geom : ogr.Geometry or shapely.geometry or list or tuple
        A vector geometry. If it is of type list/tuple representing the extent (i.e. [x_min, y_min, x_max, y_max]),
        `osr_sref` has to be given to transform the extent into a georeferenced polygon.
    osr_sref : osr.SpatialReference, optional
        Spatial reference of the given geometry `geom`.

    Returns
    -------
    ogr.Geometry
        Vector geometry as an OGR Geometry object.
    """

    if isinstance(geom, (tuple, list)) and (len(geom) == 2) and isinstance(geom[0], (tuple, list)) \
            and isinstance(geom[1], (tuple, list)):
        geom_ogr = bbox2polygon(geom, osr_sref=osr_sref)
    elif isinstance(geom, (tuple, list)) and (len(geom) == 4) and (all([isinstance(x, (float, int)) for x in geom])):
        bbox_geom = [(geom[0], geom[1]), (geom[2], geom[3])]
        geom_ogr = any_geom2ogr_geom(bbox_geom, osr_sref=osr_sref)
    elif isinstance(geom, (tuple, list)) and (len(geom) == 2) and (all([isinstance(x, (float, int)) for x in geom])):
        point = shapely.geometry.Point(geom[0], geom[1])
        geom_ogr = any_geom2ogr_geom(point)
    elif isinstance(geom, (shapely.geometry.Polygon, shapely.geometry.Point)):
        geom_ogr = ogr.CreateGeometryFromWkt(geom.wkt)
        if osr_sref is not None:
            geom_ogr.AssignSpatialReference(osr_sref)
    elif isinstance(geom, ogr.Geometry):
        geom_ogr = geom
    else:
        raise GeometryUnkown(geom)

    return geom_ogr


def xy2ij(x, y, gt):
    """
    Transforms global/world system coordinates to pixel coordinates/indexes.

    Parameters
    ----------
    x : float
        World system coordinate in X direction.
    y : float
        World system coordinate in Y direction.
    gt : tuple
        Geo-transformation parameters/dictionary.

    Returns
    -------
    i : int
        Column number in pixels.
    j : int
        Row number in pixels.
    """

    i = int(round(-1.0 * (gt[2] * gt[3] - gt[0] * gt[5] + gt[5] * x - gt[2] * y) /
                  (gt[2] * gt[4] - gt[1] * gt[5])))
    j = int(round(-1.0 * (-1 * gt[1] * gt[3] + gt[0] * gt[4] - gt[4] * x + gt[1] * y) /
                  (gt[2] * gt[4] - gt[1] * gt[5])))
    return i, j


def ij2xy(i, j, gt, origin="ul"):
    """
    Transforms global/world system coordinates to pixel coordinates/indexes.

    Parameters
    ----------
    i : int
        Column number in pixels.
    j : int
        Row number in pixels.
    gt : dict
        Geo-transformation parameters/dictionary.
    origin: str, optional
        Defines the world system origin of the pixel. It can be:
            - upper left ("ul")
            - upper right ("ur", default)
            - lower right ("lr")
            - lower left ("ll")
            - center ("c")

    Returns
    -------
    x : float
        World system coordinate in X direction.
    y : float
        World system coordinate in Y direction.
    """

    px_shift_map = {"ul": (0, 0),
                    "ur": (1, 0),
                    "lr": (1, 1),
                    "ll": (0, 1),
                    "c": (.5, .5)}

    if origin in px_shift_map.keys():
        px_shift = px_shift_map[origin]
    else:
        user_wrng = "Pixel origin '{}' unknown. Upper left origin 'ul' will be taken instead".format(origin)
        raise Warning(user_wrng)
        px_shift = (0, 0)

    i += px_shift[0]
    j += px_shift[1]
    x = gt[0] + i * gt[1] + j * gt[2]
    y = gt[3] + i * gt[4] + j * gt[5]

    return x, y

def crop_pixels2raster_geometry(raster_geom, col, row, col_size=1, row_size=1):