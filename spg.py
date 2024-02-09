import os
import math
import numpy as np
import matplotlib.pyplot as plt
from itertools import product

from load_map import load_map, extract_waypoints
from openai_models import get_embed
from utils import load_from_file


# NOTE: spot_waypoints_path :- path to the folder of files generated by GraphNav:
spot_graph_path = os.path.join(os.path.expanduser("~"), "ground", "spot", "graphs", "blackstone")

# NOTE: osm_path :- file path for the JSON file containing OSM-extracted landmarks:
osm_path = os.path.join(os.path.expanduser("~"), "ground", "data", "osm", "blackstone.json")

# NOTE: reg_output_path :- file path to the output of the RER process (obtained from Jason):
reg_output_path = os.path.join(os.path.expanduser("~"), "ground", "data", "reg_outs_blackstone.json")

KNOWN_RELATIONS = [
    'left', 'left of', 'to the left of', 'right', 'right of', 'to the right of',
    'in front of', 'opposite', 'opposite to', 'behind', 'behind of', 'at the rear of',
    'near', 'next', 'next to', 'adjacent to', 'close', 'close to', 'at', 'by', 'between',
    'north of', 'south of', 'east of', 'west of', 'northeast of', 'northwest of', 'southeast of', 'southwest of'
]

landmarks = None

# -- let's assume that we will only look for an object that is within 25m of the anchor:
MAX_RANGE = 25.0

# -- this variable indicates the offset to compute a target location for SREs without a target:
RANGE_TO_ANCHOR = 2.0

# -- this variable controls whether we use a library for converting GPS to Cartesian coordinates:
use_pyproj = True
zone = None

try:
    # -- using pyproj: https://stackoverflow.com/a/69604627
    from pyproj import Transformer
except ImportError:
    print(' >> WARNING: missing "pyproj" library for GPS->Cartesian coordinate conversion!')
    print('     - Download using "pip install pyproj".')
    use_pyproj = False

try:
    import utm
except ImportError:
    print(' >> WARNING: missing "utm" library for GPS->Cartesian coordinate conversion!')
    print('     - Download using "pip install utm".')
    use_pyproj = False


def rotation_matrix(angle):
    # Source: https://motion.cs.illinois.edu/RoboticSystems/CoordinateTransformations.html
    return np.array(
        [[np.cos(angle), -np.sin(angle)],
         [np.sin(angle), np.cos(angle)]])


def gps_to_cartesian(landmark):
    # Source: https://stackoverflow.com/questions/1185408/converting-from-longitude-latitude-to-cartesian-coordinates

    lat, long = landmark['lat'], landmark['long']

    # NOTE: radius of earth is approximately 6371 km; if we want it scaled to meters, then we should multiply by 1000:
    radius_earth = 6378.1370 * 1000
    x = radius_earth * np.cos(np.deg2rad(lat)) * np.cos(np.deg2rad(long))
    y = radius_earth * np.cos(np.deg2rad(lat)) * np.sin(np.deg2rad(long))
    z = radius_earth * np.sin(np.deg2rad(lat))
    return [x, y, z]


def compute_area(spatial_rel, anchor, do_360_search=False, plot=False):

    robot = landmarks['robot']

    # NOTE: we want to draw a vector from the anchor's perspective to the robot!
    # -- this gives us a normal vector pointing outside of the anchor object
    list_ranges = []

    # -- compute vector between robot's position and anchor position and get its direction:
    vector_a2r = [robot['x'] - anchor['x'],
                  robot['y'] - anchor['y']]

    # -- draw a unit vector and multiply it by 10 to get the max distance to consider:
    unit_vec_a2r = np.array(vector_a2r) / np.linalg.norm(vector_a2r)

    # NOTE: mean angle of 0 if we get the spatial relation "in front of" or "opposite"
    mean_angle = 0
    if spatial_rel in ['left', 'left of', 'to the left of']:
        # -- if we want something to the left, we need to go in positive 90 degrees:
        mean_angle = -90
    elif spatial_rel in ['right', 'right of', 'to the right of']:
        # -- if we want something to the right, we need to go in negative 90 degrees:
        mean_angle = 90
    elif spatial_rel in ['behind', 'at the rear of', 'behind of']:
        # -- if we want something to the right, we need to tn 180 degees:
        mean_angle = 180
    elif spatial_rel in ['north of', 'south of', 'east of', 'west of', 'northeast of', 'northwest of', 'southeast of', 'southwest of']:
        # -- we need to find the difference between each cardinal direction and the current anchor-to-robot vector
        #       to figure out how much we need to rotate it by:
        if spatial_rel in ['north', 'north of']:
            mean_angle = np.rad2deg(np.arctan2(1, 0) - np.arctan2(unit_vec_a2r[1], unit_vec_a2r[0]))
        elif spatial_rel in ['south', 'south of']:
            mean_angle = np.rad2deg(np.arctan2(-1, 0) - np.arctan2(unit_vec_a2r[1], unit_vec_a2r[0]))
        elif spatial_rel in ['east', 'east of']:
            mean_angle = np.rad2deg(np.arctan2(0, 1) - np.arctan2(unit_vec_a2r[1], unit_vec_a2r[0]))
        elif spatial_rel in ['west', 'west of']:
            mean_angle = np.rad2deg(np.arctan2(0, -1) - np.arctan2(unit_vec_a2r[1], unit_vec_a2r[0]))
        elif spatial_rel in ['northeast', 'northeast of']:
            mean_angle = np.rad2deg(np.arctan2(1, 1) - np.arctan2(unit_vec_a2r[1], unit_vec_a2r[0]))
        elif spatial_rel in ['northwest', 'northwest of']:
            mean_angle = np.rad2deg(np.arctan2(1, -1) - np.arctan2(unit_vec_a2r[1], unit_vec_a2r[0]))
        elif spatial_rel in ['southeast', 'southeast of']:
            mean_angle = np.rad2deg(np.arctan2(-1, 1) - np.arctan2(unit_vec_a2r[1], unit_vec_a2r[0]))
        elif spatial_rel in ['southwest', 'southwest of']:
            mean_angle = np.rad2deg(np.arctan2(-1, -1) - np.arctan2(unit_vec_a2r[1], unit_vec_a2r[0]))

        # NOTE: since cardinal directions are absolute, we should not do any 360-sweep:
        do_360_search = False

    # endif

    # -- this dictates how wide of a field-of-view we attribute to the robot:
    field_of_view = 180

    # -- checking for sweep condition: this means we will consider different normal vectors
    #       representing the "front" of the object:
    rot_a2r = [0]
    if spatial_rel in ['near', 'near to', 'next', 'next to', 'adjacent to', 'close to', 'at', 'close', 'by'] or do_360_search:
        rot_a2r += [x * field_of_view for x in range(1, int(360 / field_of_view))]

    # print(rot_a2r)
    for x in rot_a2r:
        # -- rotate the anchor's frame of reference by some angle x:
        a2r_vector = np.dot(rotation_matrix(angle=np.deg2rad(x)), unit_vec_a2r)

        # -- compute the mean vector as well as vectors representing min and max proximity range:
        a2r_mean = np.dot(rotation_matrix(angle=np.deg2rad(mean_angle)), a2r_vector)
        a2r_min_range = np.dot(rotation_matrix(angle=np.deg2rad(mean_angle-(field_of_view/2))), a2r_vector)
        a2r_max_range = np.dot(rotation_matrix(angle=np.deg2rad(mean_angle+(field_of_view/2))), a2r_vector)

        # -- append the vectors to the list of evaluated ranges:
        list_ranges.append({
            'mean': a2r_mean,
            'min': a2r_min_range,
            'max': a2r_max_range,
        })
    # endfor

    if plot:
        plt.figure()

        # -- plotting the robot's position as well as the anchor point:
        plt.scatter(x=[robot['x']],
                    y=[robot['y']], marker='o', color='yellow', label='robot')
        plt.scatter(x=[anchor['x']],
                    y=[anchor['y']], marker='o', color='orange', label='anchor')
        plt.text(anchor['x'], anchor['y'], s=anchor['name'])

        # -- plotting the normal vector from the robot to the anchor:
        plt.plot([robot['x'], anchor['x']],
                 [robot['y'], anchor['y']], color='black')
        plt.arrow(x=robot['x'], y=robot['y'], dx=-vector_a2r[0]/2.0, dy=-vector_a2r[1]/2.0, shape='full',
                    width=0.01, head_width=0.1, color='black', label='normal')

        for r in range(len(list_ranges)):
            mean_pose = [(list_ranges[r]['mean'][0] * MAX_RANGE) + anchor['x'],
                            (list_ranges[r]['mean'][1] * MAX_RANGE) + anchor['y']]
            plt.scatter(x=[mean_pose[0]], y=[mean_pose[1]],
                        c='g', marker='o', label=f'mean_{r}')

            min_pose = [(list_ranges[r]['min'][0] * MAX_RANGE) + anchor['x'],
                        (list_ranges[r]['min'][1] * MAX_RANGE) + anchor['y']]
            plt.scatter(x=[min_pose[0]], y=[min_pose[1]],
                        c='r', marker='x', label=f'min_{r}')

            max_pose = [(list_ranges[r]['max'][0] * MAX_RANGE) + anchor['x'],
                        (list_ranges[r]['max'][1] * MAX_RANGE) + anchor['y']]
            plt.scatter(x=[max_pose[0]], y=[max_pose[1]],
                        c='b', marker='x', label=f'max_{r}')

            plt.plot([anchor['x'], mean_pose[0]],
                     [anchor['y'], mean_pose[1]], linestyle='dashed', c='g')
            plt.plot([anchor['x'], min_pose[0]],
                     [anchor['y'], min_pose[1]], linestyle='dotted', c='r')
            plt.plot([anchor['x'], max_pose[0]],
                     [anchor['y'], max_pose[1]], linestyle='dotted', c='b')
        # endfor

        plt.title(f'Evaluated range for spatial relation "{spatial_rel}"')
        plt.legend()
        plt.axis('square')
        plt.show(block=False)

    return list_ranges


def evaluate_spg(spatial_rel, target_candidate, anchor_candidates, sre=None, plot=False):
    # -- in this case, we will be given a list of target objects or entities:
    target = landmarks[target_candidate]

    # -- we cannot evaluate a landmark against itself, so we need to check if any
    #       anchor candidates are equal to the target candidate:
    if target_candidate in anchor_candidates:
        return False

    for A in anchor_candidates:
        # -- we will check if any anchor has the same (x,y) coordinates as the target:
        if target['x'] == landmarks[A]['x'] and target['y'] == landmarks[A]['y']:
            return False

    # -- robot is listed as a landmark:
    robot = landmarks['robot']

    if spatial_rel not in ['between']:

        try:
            anchor = landmarks[anchor_candidates[0]]
        except KeyError:
            return False

        anchor['name'] = anchor_candidates[0]

        list_ranges = compute_area(spatial_rel, anchor, plot)

        is_valid = False

        for R in list_ranges:
            v_tgt = np.array([target['x'] - anchor['x'], target['y'] - anchor['y']])
            v_min = np.array([R['min'][0], R['min'][1]])
            v_max = np.array([R['max'][0], R['max'][1]])

            # -- checking if the target vector lies between the min and max vectors
            #       Source: https://stackoverflow.com/a/17497339
            is_within_vectors = bool(np.cross(v_max,v_tgt) * np.cross(v_max,v_min) >= 0) and bool(np.cross(v_min,v_tgt) * np.cross(v_min,v_max) >= 0)

            a2t_distance = np.linalg.norm(np.array([target['x'], target['y']]) - np.array([anchor['x'], anchor['y']]))

            if is_within_vectors and a2t_distance <= MAX_RANGE:
                print(f'    - VALID LANDMARKS:\ttarget:{target_candidate}\tanchor:{anchor_candidates[0]}')
                is_valid = True
                break

        if is_valid:

            if plot:
                # -- plot the computed range:
                plt.figure()
                plt.title(f'Final Grounding: "{sre}"\n(Target:{target_candidate}, Anchor:{anchor_candidates})')
                plt.scatter(x=[robot['x']], y=[robot['y']], marker='o', color='yellow', label='robot')
                plt.scatter(x=[anchor['x']], y=[anchor['y']], marker='o', color='orange', label='anchor')
                plt.scatter(x=[target['x']], y=[target['y']], marker='o', color='green', label='target')
                plt.plot([robot['x'], anchor['x']], [robot['y'], anchor['y']], linestyle='dotted', c='k', label='normal')

                plt.text(anchor['x'], anchor['y'], s=anchor_candidates[0])
                plt.text(target['x'], target['y'], s=target_candidate)

                for R in range(len(list_ranges)):
                    mean_pose = np.array([(list_ranges[R]['mean'][0] * MAX_RANGE) + anchor['x'],
                                    (list_ranges[R]['mean'][1] * MAX_RANGE) + anchor['y']])
                    # plt.scatter(x=[mean_pose[0]], y=[mean_pose[1]], c='g', marker='o', label='mean')

                    min_pose = np.array([(list_ranges[R]['min'][0] * MAX_RANGE) + anchor['x'],
                                (list_ranges[R]['min'][1] * MAX_RANGE) + anchor['y']])
                    # plt.scatter(x=[min_pose[0]], y=[min_pose[1]], c='r', marker='x', label='min')

                    max_pose = np.array([(list_ranges[R]['max'][0] * MAX_RANGE) + anchor['x'],
                                (list_ranges[R]['max'][1] * MAX_RANGE) + anchor['y']])
                    # plt.scatter(x=[max_pose[0]], y=[max_pose[1]], c='b', marker='x', label='max')

                    if R == (len(list_ranges) - 1):
                        # plt.plot([anchor['x'], mean_pose[0]], [anchor['y'], mean_pose[1]], linestyle='dotted', c='g', label='mean_range' )
                        plt.plot([anchor['x'], min_pose[0]], [anchor['y'], min_pose[1]], linestyle='dotted', c='r', label='min_range')
                        plt.plot([anchor['x'], max_pose[0]], [anchor['y'], max_pose[1]], linestyle='dotted', c='b', label='MAX_RANGE')
                    else:
                        # plt.plot([anchor['x'], mean_pose[0]], [anchor['y'], mean_pose[1]], linestyle='dotted', c='g', )
                        plt.plot([anchor['x'], min_pose[0]], [anchor['y'], min_pose[1]], linestyle='dotted', c='r')
                        plt.plot([anchor['x'], max_pose[0]], [anchor['y'], max_pose[1]], linestyle='dotted', c='b')

                plt.legend()
                plt.axis('square')
                plt.show(block=False)

            return True

    else:

        try:
            anchor_1 = landmarks[anchor_candidates[0]]
            anchor_2 = landmarks[anchor_candidates[1]]
        except KeyError:
            # -- this anchor may instead be a waypoint in the Spot's space:
            return False

        # NOTE: sometimes we may be evaluating the same anchor twice,
        #   so we need to check this before computing the relation:
        if anchor_candidates[0] == anchor_candidates[1]:
            return False

        if anchor_1['x'] == anchor_2['x'] and anchor_1['y'] == anchor_2['y']:
            return False

        target = np.array([target['x'], target['y']])
        anchor_1 = np.array([anchor_1['x'], anchor_1['y']])
        anchor_2 = np.array([anchor_2['x'], anchor_2['y']])

        # -- checking if something lies between two anchors is fairly simple: https://math.stackexchange.com/a/190373

        # -- computing vectors perpendicular to each anchoring point:
        vec_a1_to_a2 = anchor_2 - anchor_1; vec_a1_to_a2 /= np.linalg.norm(vec_a1_to_a2)
        vec_a2_to_a1 = anchor_1 - anchor_2; vec_a2_to_a1 /= np.linalg.norm(vec_a2_to_a1)
        A, B = np.dot(rotation_matrix(np.deg2rad(-90)), vec_a1_to_a2 * MAX_RANGE) + anchor_1, np.dot(rotation_matrix(np.deg2rad(90)), vec_a1_to_a2 * MAX_RANGE) + anchor_1
        C, D = np.dot(rotation_matrix(np.deg2rad(-90)), vec_a2_to_a1 * MAX_RANGE) + anchor_2, np.dot(rotation_matrix(np.deg2rad(90)), vec_a2_to_a1 * MAX_RANGE) + anchor_2

        dot_ABAM = np.dot(B-A, target-A)
        dot_ABAB = np.dot(B-A, B-A)
        dot_BCBM = np.dot(C-B, target-B)
        dot_BCBC = np.dot(C-B, C-B)

        if 0 <= dot_ABAM and dot_ABAM <= dot_ABAB and 0 <= dot_BCBM and dot_BCBM <= dot_BCBC:
            if plot:
                plt.figure()

                plt.scatter(x=[robot['x']], y=[robot['y']], marker='o', color='yellow', label='robot')
                plt.scatter(x=[target[0]], y=[target[1]], marker='o', color='green', label='target')
                plt.scatter(x=[anchor_1[0]], y=[anchor_1[1]], marker='o', color='orange', label='anchor')
                plt.scatter(x=[anchor_2[0]], y=[anchor_2[1]], marker='o', color='orange', label='anchor')

                plt.plot([A[0], anchor_1[0]], [A[1], anchor_1[1]], linestyle='dotted', c='r')
                plt.plot([C[0], anchor_2[0]], [C[1], anchor_2[1]], linestyle='dotted', c='b')
                plt.plot([B[0], anchor_1[0]], [B[1], anchor_1[1]], linestyle='dotted', c='r')
                plt.plot([D[0], anchor_2[0]], [D[1], anchor_2[1]], linestyle='dotted', c='b')

                plt.text(x=target[0], y=target[1], s=target_candidate)
                plt.text(x=anchor_1[0], y=anchor_1[1], s=anchor_candidates[0])
                plt.text(x=anchor_2[0], y=anchor_2[1], s=anchor_candidates[1])

                plt.title(f'Final Grounding: "{sre}"\n(Target:{target_candidate}, Anchor:{anchor_candidates})')
                plt.axis('square')
                plt.show(block=False)

            print(f'    - VALID LANDMARKS:\ttarget:{target_candidate}\tanchor:{anchor_candidates}')
            return True

        return False

    return False


def get_target_position(spatial_rel, anchor_candidate, sre=None, plot=False):
    # -- this means that we have no target landmark: we solely want to find a position relative to a given anchor
    try:
        anchor = landmarks[anchor_candidate]
    except KeyError:
        return None

    robot = landmarks['robot']

    # -- get the list of valid ranges (potentially only one) for an anchoring landmark:
    list_ranges = compute_area(spatial_rel, anchor)

    # -- we want to find the closest point from the robot to the anchoring landmark that satisfies the given spatial relation:
    closest_position = 0

    for R in range(len(list_ranges)):

        cur_min_pos = {'x': (list_ranges[R]['mean'][0] * RANGE_TO_ANCHOR) + anchor['x'],
                       'y': (list_ranges[R]['mean'][1] * RANGE_TO_ANCHOR) + anchor['y']}
        cur_min_dist = np.linalg.norm(np.array([cur_min_pos['x'], cur_min_pos['y']]) - np.array([robot['x'], robot['y']]))

        new_min_pos = {'x': (list_ranges[closest_position]['mean'][0] * RANGE_TO_ANCHOR) + anchor['x'],
                       'y': (list_ranges[closest_position]['mean'][1] * RANGE_TO_ANCHOR) + anchor['y']}
        new_min_dist = np.linalg.norm(np.array([new_min_pos['x'], new_min_pos['y']]) - np.array([robot['x'], robot['y']]))

        if cur_min_dist > new_min_dist:
            new_min_pos = R
    # endfor

    # -- select the index that was found to be closest to the robot:
    R = list_ranges[closest_position]

    # -- use the mean vector to find a point that is within RANGE_TO_ANCHOR (2m) of the anchor:
    new_robot_pos = {'x': (R['mean'][0] * RANGE_TO_ANCHOR) + anchor['x'],
                     'y': (R['mean'][1] * RANGE_TO_ANCHOR) + anchor['y']}

    if plot:
        plt.figure()

        plt.scatter(x=[robot['x']], y=[robot['y']], marker='o', label='robot')
        plt.scatter(x=[new_robot_pos['x']], y=[new_robot_pos['y']], marker='x', c='g', s=15, label='new robot pose')

        # -- plot all anchors and targets provided to the function:
        for A in landmarks:
            plt.scatter(x=landmarks[A]['x'],
                        y=landmarks[A]['y'],
                        marker='o', c='darkorange', label=f"anchor: {A}")
            plt.text(landmarks[A]['x'], landmarks[A]['y'], A)

        # -- plot the range as well for visualization:
        plt.plot([anchor['x'], (R['min'][0] * RANGE_TO_ANCHOR) + anchor['x']],
                 [anchor['y'], (R['min'][1] * RANGE_TO_ANCHOR) + anchor['y']],
                 linestyle='dotted', c='r')
        plt.plot([anchor['x'], (R['max'][0] * RANGE_TO_ANCHOR) + anchor['x']],
                 [anchor['y'], (R['max'][1] * RANGE_TO_ANCHOR) + anchor['y']],
                 linestyle='dotted', c='b')

        plt.title(f'Computed Target Position: "{sre}"' if sre else f'Computed Target Position: "{spatial_rel}"')
        plt.axis('square')
        plt.legend()
        plt.show(block=False)

    return new_robot_pos


def plot_landmarks(landmarks=None):
    # -- plotting the points in the shared world space local to the Spot's map:
    plt.figure()

    if landmarks:
        plt.scatter(x=[landmarks[L]['x'] for L in landmarks], y=[landmarks[L]['y'] for L in landmarks], c='green', label='landmarks')
        for L in landmarks:
            if 'osm_name' not in landmarks[L] and L != 'robot':
                plt.text(landmarks[L]['x'], landmarks[L]['y'], L)

    plt.scatter(x=landmarks['robot']['x'],
                y=landmarks['robot']['y'], c='orange', label='robot')
    plt.text(landmarks['robot']['x'],
             landmarks['robot']['y'], 'robot')

    plt.title(f'All Landmarks')
    plt.legend()
    # plt.axis('square')
    plt.show(block=True)
    # plt.savefig('temp.png')


def align_coordinates(spot_graph_dpath, osm_landmarks, spot_waypoints, coord_alignment=[], crs=None):

    # NOTE: definitions of the following variables:
    # -- angle_diff :- this dictates the amount of rotation needed to
    #       align the Spot's frame to the world Cartesian frame (default: 0 -- not needed if no robot map)
    # -- offset :- this dictates the amount of offset needed to be added to GPS coordinates from OSM
    #       (default: 0 -- not needed if no robot map)
    angle_diff = 0
    offset = 0

    # NOTE: if not using a Spot map, then there's no need for
    if bool(coord_alignment):
        # -- if grounding landmark value is not None, then we will go ahead and figure out alignment issue:

        print(' >> Computing alignment from robot to world frame...')

        if crs:
            known_landmark_1 = np.array(crs.transform(coord_alignment[0]['long'], coord_alignment[0]['lat'], 0, radians=False)[:-1])
            known_landmark_2 = np.array(crs.transform(coord_alignment[1]['long'], coord_alignment[1]['lat'], 0, radians=False)[:-1])
        else:
            known_landmark_1 = np.array([gps_to_cartesian(coord_alignment[0])[x] for x in [0, 2]])
            known_landmark_2 = np.array([gps_to_cartesian(coord_alignment[1])[x] for x in [0, 2]])

        known_waypoint_1 = np.array([spot_waypoints[coord_alignment[0]['waypoint']]['position']['x'],
                                        spot_waypoints[coord_alignment[0]['waypoint']]['position']['y']])
        known_waypoint_2 = np.array([spot_waypoints[coord_alignment[1]['waypoint']]['position']['x'],
                                        spot_waypoints[coord_alignment[1]['waypoint']]['position']['y']])

        # -- use the vector from known landmarks to determine the degree of rotation needed:
        vec_lrk_1_to_2 = known_landmark_2 - known_landmark_1
        vec_way_1_to_2 = known_waypoint_2 - known_waypoint_1

        # -- first, we will find the rotation between the known landmark and waypoint:
        dir_robot = np.arctan2(vec_way_1_to_2[1], vec_way_1_to_2[0])    # i.e., spot coordinate
        dir_world = np.arctan2(vec_lrk_1_to_2[1], vec_lrk_1_to_2[0])    # i.e., world coordinate

        angle_diff = dir_world - dir_robot

    landmarks = {}

    if not bool(spot_graph_dpath) or not(spot_waypoints):
        # -- this means we are only working with OSM landmarks:
        print(' >> WARNING: not using Spot map!')
    else:
        # -- each image is named after the Spot waypoint name (auto-generated by GraphNav):
        list_waypoints = [os.path.splitext(os.path.basename(W))[0] for W in os.listdir(os.path.join(spot_graph_dpath, 'images'))]

        for W in spot_waypoints:
            # NOTE: all landmarks are either one of the following:
            #   1. landmarks where waypoints were manually created by GraphNav (i.e., they have images)
            #   2. waypoint_0 -- this is where GraphNav starts the robot at
            is_landmark = False
            if W in list_waypoints:
                is_landmark = True
            elif spot_waypoints[W]['name'] == 'waypoint_0':
                is_landmark = True

            if is_landmark:
                # -- just get the x-coordinate and y-coordinate, which would correspond to a top-view 2D map:
                spot_coordinate = np.array([spot_waypoints[W]['position']['x'],
                                            spot_waypoints[W]['position']['y']])

                # -- align the Spot's coordinates to the world frame:
                spot_coordinate = np.dot(rotation_matrix(angle=angle_diff), spot_coordinate)

                if bool(coord_alignment):
                    # -- we will use the newly rotated point to figure out the offset:
                    if W == coord_alignment[0]['waypoint']:
                        known_waypoint_1 = spot_coordinate
                    elif W == coord_alignment[1]['waypoint']:
                        known_waypoint_2 = spot_coordinate

                landmarks[W if spot_waypoints[W]['name'] != 'waypoint_0' else 'robot'] = {
                    'x': spot_coordinate[0],
                    'y': spot_coordinate[1],
                }

        if bool(coord_alignment):
            # -- compute an offset that can be used to align the known landmark from world to Spot space AFTER rotation:
            offset = ((known_waypoint_1 - known_landmark_1) + (known_waypoint_2 - known_landmark_2)) / 2.0

    for L in osm_landmarks:
        # -- replace whitespace with underscore, make lowercase:
        id_name = str(L).lower().replace(' ', '_')

        if 'wid' not in osm_landmarks[L]:
            # -- we first need to convert each point into its Cartesian equivalent, then add the computed offset from above:
            if crs:
                landmark_cartesian = np.array(crs.transform(osm_landmarks[L]['long'], osm_landmarks[L]['lat'], 0, radians=False)[:-1])
            else:
                landmark_cartesian = np.array([gps_to_cartesian(osm_landmarks[L])[x] for x in [0, 2]])

            landmark_cartesian += offset

        else:
            # NOTE: some points in OSM will have a waypoint associated with it, so just use that x and y:
            landmark_cartesian = np.array([landmarks[osm_landmarks[L]['wid']]['x'],
                                           landmarks[osm_landmarks[L]['wid']]['y']])
            landmarks[osm_landmarks[L]['wid']]['osm_name'] = id_name

        landmarks[id_name] = {
            'x': landmark_cartesian[0],
            'y': landmark_cartesian[1],
        }

    return landmarks


def fake_spot_waypoints(spot_graph_dpath, crs=None):
    # NOTE: this function is used to create a "fake" robot map of objects similar to that created by GraphNav:

    objects = load_from_file(os.path.join(spot_graph_dpath, "objects_map.json"))

    robot = None
    if 'waypoint_0' in objects:
        robot = objects['waypoint_0']
    else:
        print(' >> ERROR: missing robot coordinates! Check your .JSON file!')
        exit()

    if not crs:
        (_, _, zone, _) = utm.from_latlon(robot['lat'], robot['long'])
        crs = Transformer.from_crs(crs_from="+proj=latlong +ellps=WGS84 +datum=WGS84",
                                   crs_to=f"+proj=utm +ellps=WGS84 +datum=WGS84 +south +units=m +zone={zone}")

    for O in objects:
        # -- we will go through each object obtained from the world map and set it to Cartesian coordinates:

        # NOTE: a 2D map actually is projected to the X-Z Cartesian plane, NOT X-Y:
        # -- for this reason, we only take the x and z coordinates, where the z will be used as Spot's y-axis:
        if crs:
            landmark_cartesian = np.array(crs.transform(objects[O]['long'], objects[O]['lat'], 0, radians=False)[:-1])
        else:
            landmark_cartesian = np.array([gps_to_cartesian(objects[O])[x] for x in [0, 2]])

        objects[O]['x'], objects[O]['y'] = landmark_cartesian[0], landmark_cartesian[1]

    waypoints = {}

    for O in objects:
        # -- we are going to add the objects as waypoints
        waypoints[O] = {
            'position': {
                # NOTE: we need to set the origin of the coordinates to the location of the robot:
                'x': objects[O]['x'],
                'y': objects[O]['y']
            },
            'name': O,
        }

    return waypoints, crs


def init(spot_graph_dpath=None, osm_landmark_file=None, do_grounding=False):
    global landmarks, osm_path, use_pyproj

    # -- load the waypoints from the provided path to Spot's map (if it exists):
    waypoints = None
    transformer = None

    try:
        (graph, _, _, _, _, _) = load_map(spot_graph_dpath)
    except Exception:
        print(' >> WARNING: no Spot graph file found in provided directory!')
        waypoints, transformer = fake_spot_waypoints(spot_graph_dpath, crs=None)
    else:
        # -- get the important details from the waypoints and create a dictionary
        #       instead of using their data structure:
        waypoints = extract_waypoints(graph)


    osm_path = osm_landmark_file

    # -- load the OSM landmarks from a JSON file:
    osm_landmarks = []
    if os.path.isfile(osm_path):
        osm_landmarks = load_from_file(osm_path)
    else:
        print(' >> WARNING: no OSM landmarks loaded!')

    # -- this is a JSON file containing a dictionary of waypoints (i.e., in Spot map) to GPS coordinates:
    alignment_lmrks = []
    alignment_fpath = os.path.join(spot_graph_dpath, "alignment.json")
    if os.path.isfile(alignment_fpath):
        alignment_lmrks = load_from_file(alignment_fpath)

    if use_pyproj:
        # -- we need to calculate a zone number for UTM conversion:
        if osm_landmarks:
            # -- get the first key in the landmarks dictionary for a single entry
            #       that we can use to get UTM zone:
            O = list(osm_landmarks.keys()).pop()
            (_, _, zone, _) = utm.from_latlon(osm_landmarks[O]['lat'],
                                              osm_landmarks[O]['long'])
            transformer = Transformer.from_crs(crs_from="+proj=latlong +ellps=WGS84 +datum=WGS84",
                                            crs_to=f"+proj=utm +ellps=WGS84 +datum=WGS84 +south +units=m +zone={zone}")

    # -- iterate through all of the Spot waypoints as well as the OSM landmarks and put them in the same space:
    landmarks = align_coordinates(spot_graph_dpath,
                                  osm_landmarks,
                                  spot_waypoints=waypoints,
                                  coord_alignment=alignment_lmrks,
                                  crs=transformer)

    # -- plot the points for visualization purposes:
    plot_landmarks(landmarks)


def sort_by_scores(spatial_pred_dict):
    # -- find Cartesian product to find all combinations of target and anchoring landmarks:
    all_products = [x for x in product(*spatial_pred_dict)]

    sorted_products = []
    for P in all_products:
        # -- compute the score attributed to a given set:
        joint_score = 1
        target, anchor = [], []
        for x in range(len(P)):
            joint_score *= P[x][0]

            # -- deriving the names of the landmarks from the dictionary:
            if x == 0:
                target.append(P[x][1])
            else:
                anchor.append(P[x][1])

        # -- save the score to a list:
        sorted_products.append({'score': joint_score, 'target': target, 'anchor': anchor})

    # -- sort everything in descending order:
    sorted_products.sort(key=lambda x: x['score'], reverse=True)

    return sorted_products


def find_match_relation(unseen_rel):
    """
    Use cosine similatiry between text embeddings to find best matching known spatil relation to the unseen input
    """
    closest_rel, closest_rel_embed = None, None
    unseen_rel_embed = get_embed(unseen_rel)

    for known_rel in KNOWN_RELATIONS:
        candidate_embed = get_embed(known_rel)

        if not closest_rel:
            closest_rel = known_rel
            closest_rel_embed = candidate_embed
        else:
            current_score = np.dot(unseen_rel_embed, closest_rel_embed)
            new_rel_score = np.dot(unseen_rel_embed, candidate_embed)

            if current_score < new_rel_score:
                closest_rel = known_rel
                closest_rel_embed = candidate_embed

    return closest_rel


def spg(spatial_preds, topk):
    print(f"Command: {spatial_preds['utt']}\n")

    global landmarks

    # -- find the closest waypoint to each anchor from OSM:
    # anchor_to_target = {}
    # for A in anchor_landmarks:
    #     this_anchor = np.array([anchor_landmarks[A]['x'], anchor_landmarks[A]['y']])

    #     closest_waypoint = None
    #     for target in target_landmarks:
    #         if not closest_waypoint:
    #             closest_waypoint = target
    #             best_waypoint = np.array([target_landmarks[closest_waypoint]['x'], target_landmarks[closest_waypoint]['y']])
    #         else:
    #             # -- check if the current target (waypoint) is closer than the closest target stored as a variable:
    #             this_waypoint = np.array([target_landmarks[target]['x'], target_landmarks[target]['y']])
    #             best_waypoint = np.array([target_landmarks[closest_waypoint]['x'], target_landmarks[closest_waypoint]['y']])

    #             # NOTE: closest in terms of Euclidean distance:
    #             if np.linalg.norm(this_anchor-this_waypoint) <= np.linalg.norm(this_anchor-best_waypoint):
    #                 closest_waypoint = target

    #     # NOTE: the best target will be the waypoint closest to the anchor:
    #     anchor_to_target[A] = closest_waypoint
    #     # print(A, anchor_to_target[A])

    spg_output = {}

    for sre, spatial_pred in spatial_preds["grounded_sre_to_preds"].items():
        print(f"Grounding SRE: {sre}")

        input_rel, lmk_grounds = list(spatial_pred.items())[0]

        # Rank all combinations of targets and anchors for computing spatial predicate grounding
        lmk_grounds_sorted = sort_by_scores(lmk_grounds)[:topk]

        breakpoint()

        groundings = []

        # Referring expression without spatial relation
        if input_rel == "None":
            groundings = [{"target": lmk_ground["target"][0]} for lmk_ground in lmk_grounds_sorted[:topk]]
        else:
            # Find best match for unseen spatial relation in set of known spatial relations
            relation = input_rel  # relation is the string that *should* be in the listed of evaluated predicates
            if input_rel not in KNOWN_RELATIONS:
                relation = find_match_relation(input_rel)
                print(f"### UNSEEN SPATIAL RELATION:\t'{input_rel}' matched to '{relation}'")

            # Spatial referring expression contains only a target landmark
            if len(lmk_grounds) == 1:
                # -- we will keep a list of target positions in order of confidence scores from REG
                for lmk_ground in lmk_grounds_sorted:
                    # NOTE: the target key will instead hold the name of the anchor in question:
                    anchor_name = lmk_ground['target'].pop()
                    groundings.append(get_target_position(relation, anchor_name, sre=sre))

            # Spatial referring expression contains a target landmark and one or two anchoring landmarks
            #   1. one anchor (e.g., <tar> left of <anc1>)
            #   2. two anchors (e.g., <tar> between <anc1> and <anc2>)
            else:
                is_valid = False

                for lmk_ground in lmk_grounds_sorted:
                    target_name = lmk_ground["target"][0]
                    anchor_names = lmk_ground["anchor"]

                    if is_valid:
                        groundings.append({"target": target_name, "anchor": anchor_names})

        spg_output[sre] = groundings

        plt.close('all')

    return spg_output


if __name__ == '__main__':
    # -- just run this part with the output from regular expression grounding (REG):
    init()
    reg_outputs = load_from_file(reg_output_path)
    for reg_output in reg_outputs:
        spg(reg_output, topk=5)
