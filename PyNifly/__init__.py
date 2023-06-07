"""NIF format export/import for Blender using Nifly"""

# Copyright © 2021, Bad Dog.

TEST_TARGET_BONE = [] # Print extra debugging info for these bones

bl_info = {
    "name": "NIF format",
    "description": "Nifly Import/Export for Skyrim, Skyrim SE, and Fallout 4 NIF files (*.nif)",
    "author": "Bad Dog",
    "blender": (3, 0, 0),
    "version": (9, 8, 0),  
    "location": "File > Import-Export",
    "support": "COMMUNITY",
    "category": "Import-Export"
}

import sys
import os
import os.path
import logging
import traceback
from mathutils import Matrix, Vector, Quaternion, geometry
import codecs

logging.basicConfig(encoding='utf-8', level=logging.DEBUG)
log = logging.getLogger("pynifly")
log.setLevel(logging.INFO)
log.info(f"Loading pynifly version {bl_info['version'][0]}.{bl_info['version'][1]}.{bl_info['version'][2]}")

nifly_path = None
if 'PYNIFLY_DEV_ROOT' in os.environ:
    pynifly_dev_root = os.environ['PYNIFLY_DEV_ROOT']
    pynifly_dev_path = os.path.join(pynifly_dev_root, r"pynifly\pynifly")
    nifly_path = os.path.join(pynifly_dev_root, r"PyNifly\NiflyDLL\x64\Debug\NiflyDLL.dll")

if nifly_path and os.path.exists(nifly_path):
    if pynifly_dev_path not in sys.path:
        sys.path.insert(0, pynifly_dev_path)
        log.setLevel(logging.INFO)
else:
    # Load from install location
    py_addon_path = os.path.dirname(os.path.realpath(__file__))
    #log.debug(f"PyNifly addon path: {py_addon_path}")
    if py_addon_path not in sys.path:
        sys.path.append(py_addon_path)
    nifly_path = os.path.join(py_addon_path, "NiflyDLL.dll")
    log.setLevel(logging.INFO)

log.info(f"Nifly DLL at {nifly_path}")
if not os.path.exists(nifly_path):
    log.error("ERROR: pynifly DLL not found")

from nifdefs import *
from niflytools import *
from pynifly import *
from trihandler import *
from blender_defs import *
import shader_io 

import bpy
import bpy_types
from bpy.props import (
        BoolProperty,
        CollectionProperty,
        EnumProperty,
        FloatProperty,
        StringProperty,
        )
from bpy_extras.io_utils import (
        ImportHelper,
        ExportHelper)
import bmesh
import skeleton_hkx
import importlib
importlib.reload(skeleton_hkx)


NO_PARTITION_GROUP = "*NO_PARTITIONS*"
MULTIPLE_PARTITION_GROUP = "*MULTIPLE_PARTITIONS*"
UNWEIGHTED_VERTEX_GROUP = "*UNWEIGHTED_VERTICES*"
ALPHA_MAP_NAME = "VERTEX_ALPHA"

CONNECT_POINT_SCALE = 1.0

COLLISION_COLOR = (0.559, 0.624, 1.0, 0.5)
collision_names = ["bhkBoxShape", "bhkConvexVerticesShape", "bhkListShape", 
                   "bhkConvexTransformShape", "bhkCapsuleShape",
                   "bhkRigidBodyT", "bhkRigidBody", "bhkCollisionObject"]


# Default values for import/export options
APPLY_SKINNING_DEF = True
CHARGEN_EXT_DEF = "chargen"
CREATE_BONES_DEF = True
EXPORT_MODIFIERS_DEF = False
EXPORT_POSE_DEF = False
IMPORT_SHAPES_DEF = True
PRESERVE_HIERARCHY_DEF = False
RENAME_BONES_DEF = True
RENAME_BONES_NIFT_DEF = False
ROLL_BONES_NIFT_DEF = False
SCALE_DEF = 1.0
WRITE_BODYTRI_DEF = False

# --------- Helper functions -------------

def LogIf(condition, text):
    if condition:
        log.debug(text)

def LogIfBone(name, text):
    LogIf(name in TEST_TARGET_BONE, text)


def is_in_plane(plane, vert):
    """ Test whether vert is in the plane defined by the three vectors in plane """
    #find the plane's normal. p0, p1, and p2 are simply points on the plane (in world space)
 
    # Get vector normal to plane
    v1 = plane[0] - plane[1]
    v2 = plane[2] - plane[1]
    normal = v1.cross(v2)
    normal.normalize()

    # Get vector from vertex to a point on the plane
    t = vert - plane[0]
    t.normalize()

    # If the dot product is 0, point is on plane
    dp = normal.dot(t)

    return round(dp, 4) == 0.0


def append_if_new(theList, theVector, errorfactor):
    """ Append vector to list if not already present (to within errorfactor) """
    for a in theList:
        if VNearEqual(a, theVector, epsilon=errorfactor):
            return
    theList.append(theVector)


def apply_scale_xf(xf:Matrix, sf:float):
    """Apply the scale factor sf to the matrix but NOT to the scale component of the matrix.
    When importing with a scale factor, verts and other elements are scaled already by the scale factor
    so it doesn't need to be part of the transform as well.
    """
    loc, rot, scale = (xf * sf).decompose()
    return MatrixLocRotScale(loc, rot, xf.to_scale())


def apply_scale_transl(xf:Matrix, sf:float) -> Matrix:
    """Apply the scale factor sf to the translation component of the matrix only."""
    loc, rot, scale = xf.decompose()
    return MatrixLocRotScale(loc*sf, rot, scale)


def pack_xf_to_buf(xf, scale_factor: float):
    """Pack a transform to a TransformBuf, applying a scale fator to translation"""
    xf_loc, xf_rot, xf_scale = xf.decompose()
    tb = TransformBuf()
    tb.store(xf_loc/scale_factor, xf_rot.to_matrix(), xf_scale)
    return tb


def armatures_match(a, b):
    """Returns true if all bones of the first armature have the same position in the second"""
    bpy.ops.object.mode_set(mode = 'OBJECT')
    #log.debug(f"<armatures_match> comparing {a.name} with {b.name}")
    for bone in a.data.bones:
        if bone.name in b.data.bones:
            if not MatNearEqual(bone.matrix_local, b.data.bones[bone.name].matrix_local):
                #log.debug(f"Bone {bone.name} positions do not match {a.name} vs {b.name}: \n{bone.matrix_local}!=\n{b.data.bones[bone.name].matrix_local}")
                return False
            elif not MatNearEqual(a.pose.bones[bone.name].matrix, b.pose.bones[bone.name].matrix):
                #log.debug(f"Bone {bone.name} pose positions do not match {a.name} vs {b.name}: \n{a.pose.bones[bone.name].matrix}!=\n{b.pose.bones[bone.name].matrix}")
                return False
            else:
                pass
        else:
            pass
    return True


# ------------- TransformBuf extensions -------

setattr(TransformBuf, "as_matrix", transform_to_matrix)

setattr(TransformBuf, "load_matrix", transform_from_matrix)

setattr(TransformBuf, "from_matrix", classmethod(make_transformbuf))

# ------ Bone handling ------

ROLL_ADJUST = 0 # -90 * pi / 180

def get_pose_blender_xf(node_xf: Matrix, game: str, scale_factor):
    """Take the given bone transform and add in the transform for a blender bone"""
    return apply_scale_transl(node_xf, scale_factor) @ game_rotations[game_axes[game]][0]
    #return apply_scale_transl(node_xf @ game_rotations[game_axes[game]][0], scale_factor)


def get_bone_global_xf(arma, bone_name, game:str, use_pose) -> Matrix:
    """ Return the global transform represented by the bone. """
    # Scale applied at this level on import, but by callor on export. Should be here for cosistency?
    if use_pose:
        bmx = arma.pose.bones[bone_name].matrix @ game_rotations[game_axes[game]][1]
    else:
        bmx = arma.data.bones[bone_name].matrix_local @ game_rotations[game_axes[game]][1]
    return bmx

def get_bone_xform(arma, bone_name, game, preserve_hierarchy, use_pose) -> Matrix:
    """Return the local or global transform represented by the bone"""
    bonexf = get_bone_global_xf(arma, bone_name, game, use_pose)

    if preserve_hierarchy:
        bparent = arma.data.bones[bone_name].parent
        if bparent:
            # Calculate the relative transform from the parent
            parent_xf = get_bone_global_xf(arma, bparent.name, game, use_pose)
            loc_xf = parent_xf.inverted() @ bonexf

            return loc_xf

    return bonexf


def is_compatible_skeleton(skin_xf:Matrix, shape:NiShape, skel:NifFile) -> bool:
    """Determine whether the given skeleton file is compatible with the shape. 

    It's compatible if the shape's bones' bind positions are the same as the
    skeleton's bones. 
    """
    if shape.has_global_to_skin:
        gts = shape.global_to_skin.as_matrix().inverted()  
    else: 
        gts = Matrix.Identity(4)
    for b in shape.bone_names:
        if b in skel.nodes:
            m1 = skin_xf @ shape.get_shape_skin_to_bone(b).as_matrix().inverted()
            m2 = skel.nodes[b].xform_to_global.as_matrix()
            if not MatNearEqual(m1, m2, 0.01):
                log.debug(f"Skeleton not compatible on {b}: \n{m1} != \n{m2}")
                return False
    return True


# ######################################################################## ###
#                                                                          ###
# -------------------------------- IMPORT -------------------------------- ###
#                                                                          ###
# ######################################################################## ###

# -----------------------------  MESH CREATION -------------------------------

def mesh_create_normals(the_mesh, normals):
    """ Create custom normals in Blender to match those on the object 
        normals = [(x, y, z)... ] 1:1 with mesh verts
        """
    if normals:
        # Make sure the normals are unit length
        # Magic incantation to set custom normals
        the_mesh.use_auto_smooth = True
        the_mesh.normals_split_custom_set([(0, 0, 0)] * len(the_mesh.loops))
        the_mesh.normals_split_custom_set_from_vertices([Vector(v).normalized() for v in normals])


def mesh_create_uv(the_mesh, uv_points):
    """ Create UV in Blender to match UVpoints from Nif
        uv_points = [(u, v)...] indexed by vertex index
        """
    new_uv = [(0,0)] * len(the_mesh.loops)
    for lp_idx, lp in enumerate(the_mesh.loops):
        vert_targeted = lp.vertex_index
        new_uv[lp_idx] = (uv_points[vert_targeted][0], 1-uv_points[vert_targeted][1])
    new_uvlayer = the_mesh.uv_layers.new(do_init=False)
    for i, this_uv in enumerate(new_uv):
        new_uvlayer.data[i].uv = this_uv

def mesh_create_partition_groups(the_shape, the_object):
    """ Create groups to capture partitions """
    mesh = the_object.data
    vg = the_object.vertex_groups
    partn_groups = []
    for p in the_shape.partitions:
        new_vg = vg.new(name=p.name)
        partn_groups.append(new_vg)
        if type(p) == FO4Segment:
            for sseg in p.subsegments:
                new_vg = vg.new(name=sseg.name)
                partn_groups.append(new_vg)
    for part_idx, face in zip(the_shape.partition_tris, mesh.polygons):
        if part_idx < len(partn_groups):
            this_vg = partn_groups[part_idx]
            for lp in face.loop_indices:
                this_loop = mesh.loops[lp]
                this_vg.add((this_loop.vertex_index,), 1.0, 'ADD')
    if len(the_shape.segment_file) > 0:
        #log.debug(f"..Putting segment file '{the_shape.segment_file}' on '{the_object.name}'")
        the_object['FO4_SEGMENT_FILE'] = the_shape.segment_file


def import_colors(mesh:bpy_types.Mesh, shape:NiShape):
    try:
        if (shape.shader_attributes.Shader_Flags_2 & ShaderFlags2.VERTEX_COLORS) \
            and shape.colors and len(shape.colors) > 0:
            #log.debug(f"Importing vertex colors for {shape.name}")
            clayer = None
            try: #Post V3.5
                clayer = mesh.color_attributes.new(type='BYTE_COLOR', domain='CORNER')
            except:
                clayer = mesh.vertex_colors.new()
            alphlayer = None
            if (shape.shader_attributes.Shader_Flags_1 & ShaderFlags1.VERTEX_ALPHA) \
                or (shape.shader_block_name == 'BSEffectShaderProperty'):
                # If we have a BSEffectShaderProperty we assume the alpha channel is used 
                # whether or not VERTEX_ALPHA is set. Some FO4 meshes seem to work this way.
                #log.debug(f"<import_colors> using alpha channel")
                try:
                    alphlayer = mesh.color_attributes.new(
                        name=ALPHA_MAP_NAME, type='BYTE_COLOR', domain='CORNER')
                except:
                    alphlayer = mesh.vertex_colors.new()
                alphlayer.name = ALPHA_MAP_NAME
        
            colors = shape.colors
            for lp in mesh.loops:
                c = colors[lp.vertex_index]
                clayer.data[lp.index].color = (c[0], c[1], c[2], 1.0)
                if alphlayer:
                    alph = colors[lp.vertex_index][3]
                    alphlayer.data[lp.index].color = [alph, alph, alph, 1.0]
    except:
        log.error(f"ERROR: Could not read colors on shape {shape.name}")


class NifImporter():
    """Does the work of importing a nif, independent of Blender's operator interface.
    filename can be a single filepath string or a list of filepaths
    """
    def __init__(self, filename, chargen="chargen", scale=1.0):

        #log.debug(f"Importing {filename} with flags {f}")
        if type(filename) == str:
            self.filename = filename
            self.filename_list = [filename]
        else:
            self.filename = filename[0]
            self.filename_list = filename

        self.create_bones = CREATE_BONES_DEF
        self.rename_bones = RENAME_BONES_DEF
        self.rename_bones_nift = RENAME_BONES_NIFT_DEF
        self.roll_bones_nift = ROLL_BONES_NIFT_DEF
        self.import_shapes = IMPORT_SHAPES_DEF
        self.apply_skinning = APPLY_SKINNING_DEF
        self.reference_skel = None
        self.chargen_ext = chargen
        self.mesh_only = False
        self.armature = None
        self.imported_armatures = []
        self.is_new_armature = True # Armature is derived from current nif; set false if adding to existing arma
        self.parent_cp = None
        self.created_child_cp = None
        self.bones = set()
        self.objects_created = {} # Dictionary of objects created, indexed by node handle
                                  # (or object name, if no handle)
        self.nodes_loaded = {} # Dictionary of nodes from the nif file loaded, indexed by Blender name
        self.loaded_meshes = [] # Holds blender objects created from shapes in a nif
        self.nif = None # NifFile(filename)
        self.collection = None
        self.loc = Vector((0, 0, 0))   # location for new objects 
        self.scale = scale
        self.warnings = []

    def add_warning(self, text:str):
        self.warnings.append(('WARNING', text))
        log.warning(text)

    def incr_loc(self):
        self.loc = self.loc + (Vector((.5, .5, .5)) * self.scale) 

    def next_loc(self):
        l = self.loc
        self.incr_loc()
        return l

    def nif_name(self, blender_name):
        if self.rename_bones or self.rename_bones_nift:
            return self.nif.nif_name(blender_name)
        else:
            return blender_name
        
    def blender_name(self, nif_name):
        if self.rename_bones or self.rename_bones_nift:
            return self.nif.dict.blender_name(nif_name)
        else:
            return nif_name

    def calc_obj_transform(self, the_shape, scale_factor=1.0) -> Matrix:
        """Returns location of the_shape ready for blender as a transform.

        scale_factor is applied to the transform but not to its scale component --
        scale_factor is used to transform vert locations so it's not needed on the
        transform.
        """
        if (type(the_shape) != NiShape) or (not the_shape.has_skin_instance):
            # Statics get transformed according to the shape's transform
            return apply_scale_xf(the_shape.transform.as_matrix(), scale_factor)

        # Global-to-skin transform is what offsets all the vertices together, e.g. so that
        # heads can be positioned at the origin. Put the reverse transform on the blender 
        # object so they can be worked on in their skinned position.
        # Use the one on the NiSkinData if it exists.
        #xform = the_shape.global_to_skin_data
        #if True: #xform is None:
        xf = Matrix.Identity(4)
        offset_consistent = False
        if the_shape.has_global_to_skin:
            # if this transform exists, use it and don't muck with it.
            xform = the_shape.global_to_skin
            #log.debug(f"Using {the_shape.name}'s global-to-skin transform: {xform.as_matrix().translation}")
            xf = xform.as_matrix().inverted()
            offset_consistent = True
        
        offset_xf = None
        if self.create_bones and self.reference_skel:
            # If we're creating missing vanilla bones, we need to know the offset from the
            # bind positions here to the vanilla bind positions, and we need it to be
            # consistent.
            #log.debug(f"Checking bone offsets from reference skel: {self.reference_skel.filepath}")
            for bn in the_shape.get_used_bones():
                if bn in self.reference_skel.nodes:
                    skel_bone = self.reference_skel.nodes[bn]
                    skel_bone_xf= skel_bone.xform_to_global.as_matrix()
                    LogIfBone(bn, f"Bone '{bn}' transform: {skel_bone_xf.translation}/{skel_bone_xf.to_euler()}")
                    bindpos = bind_position(the_shape, bn)
                    LogIfBone(bn, f"Shape {the_shape.name} bind position for '{bn}': {bindpos.translation}/{bindpos.to_euler()}")
                    bindinshape = xf @ bindpos
                    this_offset = skel_bone_xf @ bindinshape.inverted()
                    if not offset_xf: 
                        offset_xf = this_offset
                        offset_consistent = True
                        #log.debug(f"Shape {the_shape.name} first offset from {bn}: {this_offset.translation}/{this_offset.to_euler()}")
                    elif not MatNearEqual(this_offset, offset_xf):
                        offset_consistent = False
                        #log.debug(f"Shape {the_shape.name} does not have consistent offset from vanilla: {bn}:{this_offset.translation}/{this_offset.to_euler()} != {offset_xf.translation}/{offset_xf.to_euler()}")
                        break

            if offset_consistent and offset_xf:
                #log.debug(f"Shape {the_shape.name} has consistent offset from vanilla: {offset_xf.translation}")
                xf = xf @ offset_xf

        if not offset_consistent: 
            # If there's no global to skin (FO4) and we haven't found consistent bind
            # offsets, maybe the pose offsets will give us a skin transform. If they are
            # all the same they represent a simple reposition of the entire shape. We can
            # put the inverse on the Blender shape.
            pose_xf = None
            same = True
            for b in the_shape.get_used_bones():
                bone_xf = pose_transform(the_shape, b)
                if pose_xf:
                    # Some common nifs such as the Bodytalk male body need some extra
                    # fudge factor. Reducing epsilon here will result in their shape not
                    # getting adjusted to the armature location. 
                    if not MatNearEqual(pose_xf, bone_xf, epsilon=0.5):
                        #log.debug(f"Pose transform not consistent in {the_shape.name} with bone {b}:\n{pose_xf}\n!=\n{bone_xf}")
                        same = False
                        break
                else:
                    pose_xf = bone_xf
            if same: 
                #log.debug(f"Pose transforms consistent, using it for {the_shape.name}:\n{pose_xf}")
                xf = xf @ pose_xf
                xf.invert()

        #log.debug(f"Shape {the_shape.name} has calculated transform {xf.translation}")
        return apply_scale_xf(xf, scale_factor)


    # -----------------------------  EXTRA DATA  -------------------------------

    def add_to_parents(self, obj):
        """Add the given object to our list of parent connect points loaded in this operation.
        obj must be a valid BSConnectPointParents object. """
        connectname = obj.name[len('BSConnectPointParents::P-'):]
        self.loaded_parent_cp[connectname] = obj


    def add_to_child_cp(self, obj):
        """Add the given object to our list of children connect points loaded in this operation.
        obj must be a valid BSConnectPointChildren object. """
        for i in range(100):
            try:
                n = obj[f"PYN_CONNECT_CHILD_{i}"]
            except:
                break
            connectname = n[2:]
            self.loaded_child_cp[connectname] = obj


    def import_extra(self, f: NifFile):
        """ Import any extra data from the root, and create corresponding shapes 
            Returns a list of the new extradata objects
        """
        for s in f.string_data:
            bpy.ops.object.add(radius=self.scale, type='EMPTY', location=self.next_loc())
            ed = bpy.context.object
            ed.name = "NiStringExtraData"
            ed.show_name = True
            ed.empty_display_type = 'SPHERE'
            ed['NiStringExtraData_Name'] = s[0]
            ed['NiStringExtraData_Value'] = s[1]
            # extradata.append(ed)
            self.objects_created[ed.name] = ed

        for s in f.behavior_graph_data:
            bpy.ops.object.add(radius=self.scale, type='EMPTY', location=self.next_loc())
            ed = bpy.context.object
            ed.name = "BSBehaviorGraphExtraData"
            ed.show_name = True
            ed.empty_display_type = 'SPHERE'
            ed['BSBehaviorGraphExtraData_Name'] = s[0]
            ed['BSBehaviorGraphExtraData_Value'] = s[1]
            ed['BSBehaviorGraphExtraData_CBS'] = s[2]
            # extradata.append(ed)
            self.objects_created[ed.name] = ed

        for c in f.cloth_data: 
            bpy.ops.object.add(radius=self.scale, type='EMPTY', location=self.next_loc())
            ed = bpy.context.object
            ed.name = "BSClothExtraData"
            ed.show_name = True
            ed.empty_display_type = 'SPHERE'
            ed['BSClothExtraData_Name'] = c[0]
            ed['BSClothExtraData_Value'] = codecs.encode(c[1], 'base64')
            # extradata.append(ed)
            self.objects_created[ed.name] = ed

        b = f.bsx_flags
        if b:
            bpy.ops.object.add(radius=self.scale, type='EMPTY', location=self.next_loc())
            ed = bpy.context.object
            ed.name = "BSXFlags"
            ed.show_name = True
            ed.empty_display_type = 'SPHERE'
            ed['BSXFlags_Name'] = b[0]
            ed['BSXFlags_Value'] = BSXFlags(b[1]).fullname
            # extradata.append(ed)
            self.objects_created[ed.name] = ed

        invm = f.inventory_marker
        if invm:
            bpy.ops.object.add(radius=self.scale, type='EMPTY', location=self.next_loc())
            ed = bpy.context.object
            ed.name = "BSInvMarker"
            ed.show_name = True
            ed.empty_display_type = 'ARROWS'
            ed.rotation_euler = (invm[1:4])
            ed['BSInvMarker_Name'] = invm[0]
            ed['BSInvMarker_RotX'] = invm[1]
            ed['BSInvMarker_RotY'] = invm[2]
            ed['BSInvMarker_RotZ'] = invm[3]
            ed['BSInvMarker_Zoom'] = invm[4]
            # extradata.append(ed)
            self.objects_created[ed.name] = ed

        for fm in f.furniture_markers:
            bpy.ops.object.add(radius=1.0, type='EMPTY')
            obj = bpy.context.object
            obj.name = "BSFurnitureMarkerNode"
            obj.show_name = True
            obj.empty_display_type = 'SINGLE_ARROW'
            obj.location = Vector(fm.offset[:]) * self.scale
            obj.rotation_euler = (-pi/2, 0, fm.heading)
            obj.scale = Vector((40,10,10)) * self.scale
            obj['AnimationType'] = FurnAnimationType.GetName(fm.animation_type)
            obj['EntryPoints'] = FurnEntryPoints(fm.entry_points).fullname
            self.objects_created[obj.name] = obj

        for cp in f.connect_points_parent:
            #log.debug(f"Found parent connect point: \n{cp}")
            bpy.ops.object.add(radius=self.scale, type='EMPTY')
            obj = bpy.context.object
            obj.name = "BSConnectPointParents" + "::" + cp.name.decode('utf-8')
            obj.show_name = True
            obj.empty_display_type = 'ARROWS'
            mx = Matrix.LocRotScale(
                Vector(cp.translation[:]) * self.scale,
                Quaternion(cp.rotation[:]),
                ((cp.scale * CONNECT_POINT_SCALE * self.scale),) * 3
            )
            #log.debug(f"Setting location to {mx.translation}")
            obj.matrix_world = mx
            # obj.location = Vector(cp.translation[:]) * self.scale
            # obj.rotation_mode = 'QUATERNION'
            # obj.rotation_quaternion = Quaternion(cp.rotation[:])
            # obj.scale = ((cp.scale * CONNECT_POINT_SCALE * self.scale),) * 3
            #log.debug(f"New connect point {obj.name} at {obj.matrix_world.translation}")

            parname = cp.parent.decode('utf-8')

            if parname and not parname.startswith("BSConnectPointChildren") \
                and not parname.startswith("BSConnectPointParents"):
                obj["pynConnectParent"] = parname
                parnamebl = self.blender_name(parname)
                if self.armature and parnamebl in self.armature.data.bones:
                    # log.info(f"Connect point {obj.name} is parented to bone {parnamebl}")
                    parbone = self.armature.data.bones[parnamebl]
                    obj.parent = self.armature
                    obj.matrix_world = parbone.matrix_local @ obj.matrix_world
                elif parname in f.nodes:
                    parnode = f.nodes[parname]
                    if parnode._handle in self.objects_created:
                        obj.parent = self.objects_created[parnode._handle]
                        #log.debug(f"Created parent cp {obj.name} with parent {obj.parent.name}")
                    else:
                        self.add_warning(f"Parent node {parname} not imported")
                else:
                    self.add_warning(f"Could not find parent node {parname} for connect point {obj.name}")

            self.objects_created[obj.name] = obj
            self.add_to_parents(obj)

        if f.connect_points_child:
            ##log.debug(f"Found child connect point: \n{cp}")
            bpy.ops.object.add(radius=self.scale, type='EMPTY', location=self.next_loc())
            obj = bpy.context.object
            obj.name = "BSConnectPointChildren"
            obj.show_name = True
            obj.empty_display_type = 'SPHERE'
            obj.location = (0,0,0)
            obj['PYN_CONNECT_CHILD_SKINNED'] = f.connect_pt_child_skinned
            for i, n in enumerate(f.connect_points_child):
                obj[f'PYN_CONNECT_CHILD_{i}'] = n
            obj.parent = self.parent_cp
            self.created_child_cp = obj
            self.objects_created[obj.name] = obj
            self.add_to_child_cp(obj)


    def import_shape_extra(self, obj, shape):
        """ Import any extra data from the shape if given or the root if not, and create 
        corresponding shapes """
        loc = list(obj.location)
        self.incr_loc()

        for s in shape.string_data:
            bpy.ops.object.add(radius=self.scale, type='EMPTY', location=self.next_loc())
            ed = bpy.context.object
            ed.name = "NiStringExtraData"
            ed.show_name = True
            ed['NiStringExtraData_Name'] = s[0]
            ed['NiStringExtraData_Value'] = s[1]
            ed.parent = obj
            self.objects_created[ed.name] = ed

        for s in shape.behavior_graph_data:
            bpy.ops.object.add(radius=self.scale, type='EMPTY', location=self.next_loc())
            ed = bpy.context.object
            ed.name = "BSBehaviorGraphExtraData"
            ed.show_name = True
            ed['BSBehaviorGraphExtraData_Name'] = s[0]
            ed['BSBehaviorGraphExtraData_Value'] = s[1]
            ed.parent = obj
            self.objects_created[ed.name] = ed


    def bone_in_armatures(self, bone_name):
        """Determine whether a bone is in one of the armatures we've imported.
        Returns the bone or None.
        """
        for arma in self.imported_armatures:
            if bone_name in arma.data.bones:
                return arma.data.bones[bone_name]
        return None


    def import_ninode(self, arma, ninode, p=None):
        """Create Blender representation of an NiNode

        Don't import the node if (1) it's already been imported, (2) it's been imported as
        a bone in the skeleton, or (3) it's the root node
        
        * arma = armature to add the bone to; may be None
        * ninode = nif node
        * p = Blender parent for new object
        * Returns the Blender representation of the node, either an object or a bone, or
          none
        """
        obj = None
        if ninode.name == ninode.file.rootName:
            return None

        # Nothing to do if we've already imported this object. 
        bl_name = self.blender_name(ninode.name)
        if ninode._handle in self.objects_created:
            return self.objects_created[ninode._handle]

        bn = self.bone_in_armatures(bl_name)
        if bn: 
            return bn 

        skelbone = None
        LogIfBone(ninode.name, f"Checking {ninode.name} in {self.reference_skel.filepath if self.reference_skel else 'None'}")
        if self.reference_skel and ninode.name in self.reference_skel.nodes:
            skelbone = self.reference_skel.nodes[ninode.name]

        elif ninode.file.game == "FO4" and ninode.name in fo4FaceDict.byNif:
            skelbone = fo4FaceDict.byNif[ninode.name]

        #log.debug(f"Found for {ninode.name} {skelbone} to add to {arma}")
        if skelbone and arma:
            # Have not created this as bone in an armature already AND it's a known
            # skeleton bone, AND we have an armature, create it as an armature bone even
            # tho it's not used in the shape
            #log.debug(f"Creating bone for {bl_name}")
            ObjectSelect([arma])
            ObjectActive(arma)
            bpy.ops.object.mode_set(mode = 'EDIT')
            bn = self.add_bone_to_arma(arma, self.blender_name(ninode.name), ninode.name)
            bpy.ops.object.mode_set(mode = 'OBJECT')
            return bn

        # If not a known skeleton bone, just import as an EMPTY object
        bpy.ops.object.add(radius=1.0, type='EMPTY')
        obj = bpy.context.object
        obj.name = ninode.name
        obj["pynBlock_Name"] = ninode.blockname
        obj.matrix_local = apply_scale_transl(ninode.transform.as_matrix(), self.scale)
        if p:
            if type(p) == bpy_types.Bone:
                # Can't set a bone as parent, but get the node in the right position
                LogIfBone(ninode.name, f"Setting node {ninode.name} to global position: \n{ninode.xform_to_global}")
                obj.matrix_local = apply_scale_xf(ninode.xform_to_global.as_matrix(), self.scale) 
            else:
                obj.parent = p
        self.objects_created[ninode._handle] = obj

        if ninode.collision_object:
            #log.debug(f"{ninode.name} has collision object")
            self.import_collision_obj(ninode.collision_object, obj)

        return obj


    def import_node_parents(self, arma, node: NiNode):
        """Import the chain of parents of the given node all the way up to the root"""
        # Get list of parents of the given node from the list, bottom-up. 
        parents = []
        n = node.parent
        while n:
            parents.insert(0, n)
            n = n.parent

        # Create the parents top-down
        obj = None
        p = None
        for ch in parents[1:]: # [0] is the root node
            obj = self.import_ninode(arma, ch, p)
            p = obj

        return obj


    def import_loose_ninodes(self, nif, arma=None):
        """Import any NiNodes that don't have any special purpose--likely skeleton bones
        that aren't used in shapes.
        """
        original_bones = set()
        if arma:
            for n in arma.data.bones.keys():
                original_bones.add(n)

        for n in nif.nodes.values():
            p = self.import_node_parents(arma, n)
            self.import_ninode(arma, n, p)
        
        if arma:
            # Set the pose position for the bones we just added
            new_bones = set(arma.data.bones.keys()).difference(original_bones)
            bone_names = [(self.nif_name(n), n) for n in new_bones]
            #log.debug(f"Setting pose locations for {bone_names}")
            self.set_bone_poses(arma, nif, bone_names)


    def mesh_create_bone_groups(self, the_shape, the_object):
        """ Create groups to capture bone weights """
        vg = the_object.vertex_groups
        for bone_name in the_shape.bone_names:
            new_vg = vg.new(name=self.blender_name(bone_name))
            for v, w in the_shape.bone_weights[bone_name]:
                new_vg.add((v,), w, 'ADD')
    

    def import_shape(self, the_shape: NiShape):
        """ Import the shape to a Blender object, translating bone names if requested
            
        * self.objects_created = List of objects created, extended with objects associated
          with this shape. Might be more than one because of extra data nodes.
        * self.loaded_meshes = List of Blender objects created that represent meshes,
          extended with this shape.
        * self.nodes_loaded = Dictionary mapping blender name : NiShape from nif
        """
        v = the_shape.verts
        t = the_shape.tris
        if self.scale == 1.0:
            v = the_shape.verts
        else:
            v = [(n[0]*self.scale, n[1]*self.scale, n[2]*self.scale) for n in the_shape.verts]

        new_mesh = bpy.data.meshes.new(the_shape.name)
        new_mesh.from_pydata(v, [], t)
        new_mesh.update(calc_edges=True, calc_edges_loose=True)
        new_object = bpy.data.objects.new(the_shape.name, new_mesh)
        self.loaded_meshes.append(new_object)
        self.nodes_loaded[new_object.name] = the_shape
        #log.debug(f"Importing new object {new_object.name}, min z = {min(v.co.z for v in new_mesh.vertices)}")
    
        if not self.mesh_only:
            self.objects_created[the_shape._handle] = new_object
            
            import_colors(new_mesh, the_shape)

            # log.info(f"import flags: {self.flags}")
            parent = self.import_node_parents(None, the_shape) 

            # Set the object transform to reflect the skin transform in the nif. This
            # positions the object conveniently for editing.
            new_object.matrix_world = self.calc_obj_transform(the_shape, 
                                                              scale_factor=self.scale)
            if parent:
                new_object.parent = parent

            #log.debug("Creating UVs")
            mesh_create_uv(new_object.data, the_shape.uvs)
            #log.debug("Creating bone groups")
            self.mesh_create_bone_groups(the_shape, new_object)
            #log.debug("Creating partition groups")
            mesh_create_partition_groups(the_shape, new_object)
            for f in new_mesh.polygons:
                f.use_smooth = True

            #log.debug("Validating mesh")
            new_mesh.validate(verbose=True)

            #log.debug("Creating normals")
            if the_shape.normals:
                mesh_create_normals(new_object.data, the_shape.normals)

            #log.debug("Creating material")
            shader_io.ShaderImporter().import_material(new_object, the_shape)
            #log.debug("Creating material DONE")
        
            # Root block type goes on the shape object because there isn't another good place
            # to put it.
            f = the_shape.file
            root = f.nodes[f.rootName]
            if root.blockname != "NiNode":
                new_object["pynRootNode_BlockType"] = root.blockname
            new_object["pynRootNode_Name"] = root.name
            new_object["pynRootNode_Flags"] = RootFlags(root.flags).fullname

            if the_shape.collision_object:
                #log.debug("Importing collisions")
                self.import_collision_obj(the_shape.collision_object, new_object)

            #log.debug("Importing extra data")
            self.import_shape_extra(new_object, the_shape)

            new_object['PYN_GAME'] = self.nif.game
            if self.scale != SCALE_DEF: new_object['PYN_SCALE_FACTOR'] = self.scale 
            new_object['PYN_RENAME_BONES'] = self.rename_bones 
            if self.rename_bones_nift != RENAME_BONES_NIFT_DEF:
                new_object['PYN_RENAME_BONES_NIFT'] = self.rename_bones_nift 


    # ------ ARMATURE IMPORT ------

    def calc_skin_transform(self, arma, obj=None) -> Matrix:
        """Determine the skin transform to use for this shape.
        Skin transform will be:
        - the transform on the armature if there is one, combined with the shape's own skin 
        transform
        - the skin transform on the shape if there is one
        - the identity matrix
        """
        skin_xf = Matrix.Identity(4)
        if not obj:
            if 'PYN_TRANSFORM' in arma:
                skin_xf = eval(arma['PYN_TRANSFORM'])
            return skin_xf

        if 'PYN_TRANSFORM' not in arma:
            skin_xf = obj.matrix_world.copy()
            arma['PYN_TRANSFORM'] = repr(skin_xf)
        else:
            try:
                # If the object is being parented to an existing armature, use the skin
                # transform the armature used.
                arma_xf = eval(arma['PYN_TRANSFORM'])
                skin_xf = obj.matrix_world
                if not MatNearEqual(arma_xf, skin_xf): 
                    log.debug(f"Transforms don't match between {arma.name} and {obj.name}" + f"\n{arma_xf.translation} != {skin_xf.translation}")
                    self.add_warning(f"Skin transform on {obj.name} do not match existing armature. Shapes may be offset.")
            except Exception as e:
                self.add_warning(repr(e))
                skin_xf = obj.matrix_world

        return skin_xf


    def bone_nif_to_blender(self, shape:NiShape, bone:str, skin_xf:Matrix) -> Matrix:
        """Return bone's final position in blender
        
        arma: armature that will parent bone
        skin_xf: the skin transform applied to all shapes under the armature.
        """
        bone_xf = shape.get_shape_skin_to_bone(bone).as_matrix()
        LogIfBone(bone, f"Bone_xf: \n{bone_xf}")
        LogIfBone(bone, f"Skin_xf: \n{skin_xf}")
        bone_xf = apply_scale_transl(skin_xf, 1/self.scale) @ bone_xf.inverted()
        LogIfBone(bone, f"Scaled bone_xf: \n{bone_xf}")
        LogIfBone(bone, f"Game rotation:\n{game_rotations[game_axes[shape.file.game]][0]}")
        bone_xf = Matrix.Scale(self.scale, 4) @ bone_xf @ game_rotations[game_axes[shape.file.game]][0]
        return bone_xf
    

    def find_compatible_arma(self, obj, armatures:list):
        """Look through the list of armatures and find one that can be used by the shape. 

        For an armature to be compatible with a shape's skin, the bind positions of the
        bones in the skin have to be the same as the edit positions of the bones in the
        armature. 

        If there's not a match, it may be that the bind positions were all offset by the 
        same amount--just a transpose. If so, we could add this transpose to the skin
        transform and then we can use the same armature.

        Returns (armature, transform-matrix), or None.
        """
        #log.debug(f"<find_compatible_arma> for {obj.name} in {[x.name for x in armatures] if armatures else armatures}")
        shape = self.nodes_loaded[obj.name]

        for arma in armatures:
            is_ok = True
            offset_xf = None
            offset_consistent = True

            for b in shape.bone_names:
                blend_name = self.blender_name(b)
                if blend_name in arma.data.bones:
                    shape_bone_xf = obj.matrix_world @ apply_scale_xf(bind_position(shape, b), self.scale) # shape.get_shape_skin_to_bone(b).as_matrix()
                    arma_xf = get_bone_xform(arma, blend_name, shape.file.game, False, False)
                    LogIfBone(b, f"<find_compatible_arma> Shape {b}: {shape.name} = {shape_bone_xf.translation}")
                    LogIfBone(b, f"<find_compatible_arma> Armature {blend_name}: {arma.name} = {arma_xf.translation}")
                    if not MatNearEqual(shape_bone_xf, arma_xf):
                        is_ok = False
                        this_offset = shape_bone_xf @ arma_xf 
                        if offset_xf:
                            if not MatNearEqual(this_offset, offset_xf):
                                offset_consistent = False
                                #log.debug(f"Offsets different for {b}: {this_offset.translation} != {offset_xf.translation}")
                                break
                        else:
                            offset_xf = this_offset
            if is_ok:
                #log.debug(f"Armature {arma.name} ok for shape {shape.name} with offset {offset_xf.translation if offset_xf else 'Identity'}")
                return arma, offset_xf
            if False and offset_consistent: ### TODO decide if we can do this
                #log.debug(f"Armature {arma.name} NOT ok, but offset consistent: {offset_xf.translation}")
                return arma, offset_xf
            else:
                #log.debug(f"Armature {arma.name} NOT ok, inconsistent offsets")
                return None, None
        return None, None


    def add_bone_to_arma(self, arma, bone_name:str, nifname:str):
        """Add bone to armature. Bone may come from nif or reference skeleton.
        Bind position is set to vanilla bind position if we're extending the skeleton.
        Otherwise set to the position in the nif. Pose position is not set--do that with
        set_bone_poses afterwards. Blender gets crashy if this isn't done in a separate
        step.

        *   bone_name = name to use for the bone in blender 
        *   nifname = name the bone has in the nif returns new bone
        """
        armdata = arma.data

        if bone_name in armdata.edit_bones:
            return None
    
        # Use the transform from the reference skeleton if we're extending bones; 
        # otherwise use the one in the file.
        if self.create_bones and self.reference_skel and nifname in self.reference_skel.nodes:
            LogIfBone(bone_name, f"Creating {bone_name} using reference location")
            bone_xform = self.reference_skel.nodes[nifname].xform_to_global.as_matrix()
            bone = create_bone(armdata, bone_name, bone_xform, 
                               self.nif.game, self.scale, 0)
        else:
            xf = self.nif.get_node_xform_to_global(nifname) 
            LogIfBone(bone_name, f"<add_bone_to_arma> creating bone {nifname} with position \n{xf}")
            bone_xform = xf.as_matrix()
            arma_xf = self.calc_skin_transform(arma)
            scaled_xf = apply_scale_transl(arma_xf, 1/self.scale)
            bone = create_bone(armdata, bone_name, scaled_xf @ bone_xform, 
                               self.nif.game, self.scale, 0)

        return bone
    

    def set_bone_poses(self, arma, nif:NifFile, bonelist:list):
        """Set the pose transform of all the given bones. Pose transform is the transform
        on the NiNode in the nif being imported.
        *   bonelist = [(nif-name, blender-name), ...]
        """
        for bn, blname in bonelist:
            if bn in nif.nodes and blname in arma.pose.bones:
                nif_bone = nif.nodes[bn]
                if nif_bone.blockname == "NiNode" and nif_bone.name != nif.rootName:
                    bone_xf = nif_bone.xform_to_global.as_matrix() 
                    pb_xf = apply_scale_transl(bone_xf, self.scale)
                    LogIfBone(bn, f"Bone '{bn}' has base transform in nif: \n{bone_xf}")
                    LogIfBone(bn, f"Bone '{bn}' has pose transform: \n{pb_xf}")
                    pose_bone = arma.pose.bones[blname]
                    pbmx = get_pose_blender_xf(bone_xf, self.nif.game, self.scale)
                    LogIfBone(bn, f"Bone '{bn}' in has Blender pose transform: \n{pbmx}")
                    pose_bone.matrix = pbmx
                    bpy.context.view_layer.update()
                    LogIfBone(bn, f"Resulting transform: {pose_bone.matrix.translation}")


    def set_all_bone_poses(self, arma, nif:NifFile):
        """Set all bone pose transforms based on the nif. No reason not to do it once at
        the end.
        """
        bonelist = [(self.nif_name(b.name), b.name) for b in arma.data.bones]
        self.set_bone_poses(arma, nif, bonelist)


    def connect_armature(self, arma):
        """ Connect up the bones in an armature to make a full skeleton.
            Use parent/child relationships in the nif if present, from the skel otherwise.
            Uses flags
                CREATE_BONES - add bones from skeleton as needed
                RENAME_BONES - rename bones to conform with blender conventions
                RENAME_BONES_NIFTOOLS - rename bones to conform with blender conventions
            Returns list of bone nodes with collisions found along the way
            """
        #log.debug(f"<connect_armature> {arma.name}={arma.data.bones.keys()}")
        ObjectActive(arma)
        
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.mode_set(mode='EDIT')

        arm_data = arma.data
        arm_data.edit_bones.update()
        bones_to_parent = [b.name for b in arm_data.edit_bones]
        new_bones = []
        collisions = set()

        i = 0
        while i < len(bones_to_parent): # list will grow while iterating
            bonename = bones_to_parent[i]
            LogIfBone(bonename, f"<connect_armature> checking {bonename}")
            arma_bone = arm_data.edit_bones[bonename]

            if arma_bone.parent is None:
                parentname = None
                parentnifname = None
                
                # look for a parent in the nif
                nifname = self.nif_name(bonename)
                if nifname in self.nif.nodes:
                    thisnode = self.nif.nodes[nifname]
                    if thisnode.collision_object:
                        collisions.add(thisnode)

                    niparent = thisnode.parent
                    if niparent and niparent.name != self.nif.rootName:
                        try:
                            parentnifname = niparent.nif_name
                        except:
                            parentnifname = niparent.name
                        parentname = self.blender_name(niparent.name)
                        #log.debug(f"Found parent in armature: {parentname}/{parentnifname} for {bonename}/{nifname}")

                LogIfBone(bonename, f"connect_armature found {parentname}, creating bones {self.create_bones}, is facebones {is_facebone(bonename)} ")
                if parentname is None and self.create_bones and not is_facebone(bonename):
                    ##log.debug(f"No parent for '{nifname}' in the nif. If it's a known bone, get parent from skeleton")
                    if self.reference_skel and \
                        nifname in self.reference_skel.nodes and \
                            nifname != self.reference_skel.rootName:
                        LogIfBone(bonename, f"Found bone in dict: {bonename}")
                        p = self.reference_skel.nodes[nifname].parent
                        if p and p.name != self.reference_skel.rootName:
                            parentname = self.blender_name(p.name)
                            parentnifname = p.name
                            LogIfBone(bonename, f"Found parent {parentname} in bone dictionary")
            
                # if we got a parent from somewhere, hook it up
                if parentname:
                    if parentname not in arm_data.edit_bones:
                        # Add parent bones and put on our list so we can get its parent
                        #log.debug(f"<connect_armature> adding bone {parentname}/{parentnifname}")
                        new_parent = self.add_bone_to_arma(arma, parentname, parentnifname)
                        bones_to_parent.append(parentname)  
                        arm_data.edit_bones[bonename].parent = new_parent
                        new_bones.append((parentnifname, parentname))
                    else:
                        arm_data.edit_bones[bonename].parent = arm_data.edit_bones[parentname]

                        # if saved_pose:
                        #     arma.pose.bones[bonename].matrix = saved_pose 
            i += 1

        bpy.ops.object.mode_set(mode='OBJECT')
        arma.update_from_editmode()
        self.set_all_bone_poses(arma, self.nif)
        bpy.ops.object.mode_set(mode='OBJECT')

        for bonenode in collisions:
            self.import_collision_obj(bonenode.collision_object, arma, bonenode)
        return collisions


    def roll_bones(self, arma):
        ObjectSelect([arma])
        ObjectActive(arma)
        bpy.ops.object.mode_set(mode='EDIT')
        # print(f"Bone roll for 'NPC Calf [Clf].L' = {arma.data.edit_bones['NPC Calf [Clf].L'].roll}")
        for b in arma.data.edit_bones:
            b.roll += -90 * pi / 180
        # print(f"Bone roll for 'NPC Calf [Clf].L' = {arma.data.edit_bones['NPC Calf [Clf].L'].roll}")
        bpy.ops.object.mode_set(mode='OBJECT')
        arma.update_from_editmode()


    
    def add_bones_to_arma(self, arma, nif, bone_names):
        """Add all the bones in the list to the armature.
        * bone_names = nif bone names to import
        """
        ObjectSelect([arma])
        ObjectActive(arma)
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.mode_set(mode='EDIT')
        new_bones = []
        for bone_nif_name in bone_names:
            if bone_nif_name != nif.rootName:
                name = self.blender_name(bone_nif_name)
                self.add_bone_to_arma(arma, name, bone_nif_name)
                new_bones.append((bone_nif_name, name))
        self.set_bone_poses(arma, nif, new_bones)
        bpy.ops.object.mode_set(mode='OBJECT')
        arma.update_from_editmode()


    def make_armature(self, the_coll: bpy_types.Collection, name_prefix=""):
        """Make a Blender armature from the given info. 
            
            Inputs:
            *   the_coll = Collection to put the armature in. 
            *   bone_names = bones to include in the armature.
            *   self.armature = existing armature to add the new bones to. May be None.
            
            Returns: 
            * new armature, set as active object
            """
        arm_data = bpy.data.armatures.new(name_prefix + self.nif.rootName)
        arma = bpy.data.objects.new(self.nif.rootName, arm_data)
        the_coll.objects.link(arma)

        ObjectActive(arma)
        ObjectSelect([arma])
        bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
        bpy.ops.object.mode_set(mode='EDIT', toggle=False)

        if self.nif.dict.use_niftools:
            try:
                arm_data.niftools.axis_forward = "Z"
                arm_data.niftools.axis_up = "-X"
            except:
                pass

        if self.scale != SCALE_DEF: arma['PYN_SCALE_FACTOR'] = self.scale 
        arma['PYN_RENAME_BONES'] = self.rename_bones
        if self.rename_bones_nift != RENAME_BONES_NIFT_DEF:
            arma['PYN_RENAME_BONES_NIFTOOLS'] = self.rename_bones_nift 

        return arma


    def connect_to_arma(self, arma, obj):
        """Do the actual work of connecting obj to arma. If obj has no parent, parent to the arma; 
        if it does, just create an arma modifier.
        """
        if obj.parent:
            #log.debug(f"<connect_to_arma> Connecting {obj.name} to {arma.name}")
            ObjectActive(obj)
            i = len(obj.modifiers)
            bpy.ops.object.modifier_add(type='ARMATURE')
            mod = obj.modifiers[i]
            mod.object = arma
        else:
            ObjectSelect([obj])
            ObjectActive(arma)
            #log.debug(f"<connect_to_arma> Setting parent of {obj.name} to {arma.name} (with transforms)")
            bpy.ops.object.parent_set(type='ARMATURE_NAME', xmirror=False, keep_transform=False)


    def set_parent_arma(self, arma, obj, nif_shape:NiShape, s2a_xf:Matrix):
        """Set the given armature as parent of the given object.
        
        * arma - armature to use as parent. May be None, in which case it's created if
          necessary.
        * obj - skinned shape. Bones it uses are added to the arma at bind position, with
          the nif location as pose position
        * nif_shape - corresponding shape from the nif
        * s2a_xf - additional transform which must be applied to import this shape under
          this armature (may be None)
        
        Returns armature with bones from this shape added
        """
        #log.debug(f"<set_parent_arma> Skinning and parenting object {obj.name} at location {obj.location}")
        if arma is None:
            arma = self.make_armature(self.collection, "PYN_IMPORT_ARMA.")

        # All shapes parented to the same armature need to have the same transform applied
        # for editing. (Puts body parts in a convenient place for editing.) That transform
        # is stored on the shape.
        unscaled_skin_xf = self.calc_skin_transform(arma, obj)
        #log.debug(f"Found skin transform on {obj.name} = {unscaled_skin_xf.translation}")
        if s2a_xf:
            # unscaled_skin_xf = s2a_xf @ unscaled_skin_xf
            unscaled_skin_xf = unscaled_skin_xf.inverted() @ s2a_xf 

        obj.matrix_world = unscaled_skin_xf
        skin_xf = apply_scale_transl(unscaled_skin_xf, 1/self.scale)

        # Create bones reflecting the skin-to-bone transforms of the shape (bind position).
        ObjectActive(arma)
        new_bones = []
        bpy.ops.object.mode_set(mode = 'EDIT')
        if not self.reference_skel:
            self.add_warning(f"{nif_shape.name} has no reference skeleton")
            ref_compat = None
        else:
            ref_compat = is_compatible_skeleton(skin_xf, nif_shape, self.reference_skel)
            if not ref_compat:
                self.add_warning(f"{nif_shape.name} is not compatible with skeleton {self.reference_skel.filepath}")
            for bn in nif_shape.bone_names:
                blname = self.blender_name(bn)
                if blname not in arma.data.edit_bones:
                    if self.create_bones and bn in self.reference_skel.nodes and ref_compat:
                        bone_shape_xf = self.reference_skel.nodes[bn].xform_to_global.as_matrix()
                        xf = bone_shape_xf
                    else:
                        bone_shape_xf = nif_shape.get_shape_skin_to_bone(bn).as_matrix().inverted()
                        xf = skin_xf @ bone_shape_xf
                    LogIfBone(bn, f"Bone '{bn}' has shape {obj.name} xform matrix \n{skin_xf}")
                    LogIfBone(bn, f"Bone '{bn}' has skin-to-bone xform matrix \n{bone_shape_xf}")
                    LogIfBone(bn, f"Bone '{bn}' has final matrix \n{xf}")
                    create_bone(arma.data, blname, xf, self.nif.game, self.scale, 0)
                    new_bones.append((bn, blname))

        # Do the pose in a separate pass so we don't have to flip between modes.
        bpy.ops.object.mode_set(mode = 'OBJECT')
        self.set_bone_poses(arma, self.nif, new_bones)
        bpy.ops.object.mode_set(mode = 'OBJECT')

        ObjectSelect([obj])
        ObjectActive(arma)
        bpy.ops.object.parent_set(type='ARMATURE_NAME', xmirror=False, keep_transform=False)
        ObjectActive(obj)

        return arma


    # ------- COLLISION IMPORT --------

    def import_bhkConvexTransformShape(self, cs:CollisionShape, cb:bpy_types.Object):
        bpy.ops.object.add(radius=1.0, type='EMPTY')
        cshape = bpy.context.object
        cshape['bhkMaterial'] = SkyrimHavokMaterial.get_name(cs.properties.bhkMaterial)
        cshape['bhkRadius'] = cs.properties.bhkRadius * self.scale
        xf = Matrix(cs.transform)
        xf.translation = xf.translation * HAVOC_SCALE_FACTOR * self.scale
        cshape.matrix_local = xf

        self.import_collision_shape(cs.child, cshape)

        return cshape


    def import_bhkListShape(self, cs:CollisionShape, cb:bpy_types.Object):
        """ Import collision list. cs=collision node in nif. cb=collision body in Blender """
        bpy.ops.object.add(radius=1.0, type='EMPTY')
        cshape = bpy.context.object
        cshape.show_name = True
        cshape['bhkMaterial'] = SkyrimHavokMaterial.get_name(cs.properties.bhkMaterial)

        for child in cs.children:
            self.import_collision_shape(child, cshape)

        return cshape

    def import_bhkBoxShape(self, cs:CollisionShape, cb:bpy_types.Object):
        m = bpy.data.meshes.new(cs.blockname)
        prop = cs.properties
        sf = HAVOC_SCALE_FACTOR * self.scale * game_collision_sf[self.nif.game]
        dx = prop.bhkDimensions[0] * sf
        dy = prop.bhkDimensions[1] * sf
        dz = prop.bhkDimensions[2] * sf
        v = [ [-dx, dy, dz],    
              [-dx, -dy, dz],   
              [-dx, -dy, -dz],  
              [-dx, dy, -dz],
              [dx, dy, dz],
              [dx, -dy, dz],
              [dx, -dy, -dz],
              [dx, dy, -dz] ]
        ##log.debug(f"Creating shape with vertices: {v}")
        m.from_pydata(v, [], 
                      [ (0, 1, 2, 3), 
                        (4, 5, 6, 7),
                        (0, 1, 5, 4),
                        (2, 3, 7, 6),
                        (0, 4, 7, 3), 
                        (5, 1, 2, 6)])
        obj = bpy.data.objects.new(cs.blockname, m)
        # obj.matrix_world = cb.matrix_world
        bpy.context.view_layer.active_layer_collection.collection.objects.link(obj)
        # bpy.context.scene.collection.objects.link(obj)
        obj['bhkMaterial'] = SkyrimHavokMaterial.get_name(prop.bhkMaterial)
        obj['bhkRadius'] = prop.bhkRadius * self.scale

        return obj
        
    def import_bhkCapsuleShape(self, cs:CollisionShape, cb:bpy_types.Object):
        prop = cs.properties
        p1 = Vector(prop.point1)
        p2 = Vector(prop.point2)
        vaxis = p2 - p1
        #log.debug(f"Creating capsule shape between {p1} and {p2}")
        sf = HAVOC_SCALE_FACTOR * self.scale * game_collision_sf[self.nif.game]
        shapelen = vaxis.length * sf
        shaperad = prop.radius1 * sf

        bpy.ops.mesh.primitive_cylinder_add(radius=shaperad, depth=shapelen)
        obj = bpy.context.object

        q = Quaternion((1,0,0), -pi/2)
        objtrans, objrot, objscale = obj.matrix_world.decompose()
        objrot.rotate(q)
        sf = HAVOC_SCALE_FACTOR * self.scale * game_collision_sf[self.nif.game]
        objtrans = Vector(( (((p2.x - p1.x)/2) + p1.x) * sf,
                            (((p2.y - p1.y)/2) + p1.y) * sf,
                            (((p2.z - p1.z)/2) + p1.z) * sf,
                            ))
        
        obj.matrix_world = MatrixLocRotScale(objtrans, objrot, objscale)

        for p in obj.data.polygons:
            p.use_smooth = True
        obj.data.update()
        
        # bpy.context.view_layer.active_layer_collection.collection.objects.link(obj)
        obj['bhkMaterial'] = SkyrimHavokMaterial.get_name(prop.bhkMaterial)
        obj['bhkRadius'] = prop.bhkRadius * self.scale
        return obj
        

    def show_collision_normals(self, cs:CollisionShape, cso):
        #norms = [Vector(n)*HAVOC_SCALE_FACTOR for n in cs.normals]
        sf = -HAVOC_SCALE_FACTOR * self.scale * game_collision_sf[self.nif.game]
        bpy.ops.object.select_all(action='DESELECT')
        for n in cs.normals:
            bpy.ops.object.add(radius=1.0, type='EMPTY')
            obj = bpy.context.object
            obj.empty_display_type = 'SINGLE_ARROW'
            obj.empty_display_size = n[3] * sf
            v = Vector(n)
            v.normalize()
            q = Vector((0,0,1)).rotation_difference(v)
            obj.rotation_mode = 'QUATERNION'
            obj.rotation_quaternion = q
            obj.parent = cso
            

    def import_bhkConvexVerticesShape(self, 
                                      collisionnode:CollisionShape,
                                      collisionbody:bpy_types.Object):
        """Import a bhkConvexVerticesShape object.
            collisionnode = the bhkConvexVerticesShape node in the nif
            collisionbody = parent collision body object in Blender 
        """
        prop = collisionnode.properties

        sf = HAVOC_SCALE_FACTOR * self.scale * game_collision_sf[self.nif.game]

        #log.debug(f"Convex verts bounds X RAW: {min(v[0] for v in collisionnode.vertices)}, {max(v[0] for v in collisionnode.vertices)}")
        sourceverts = [Vector(v[0:3])*sf for v in collisionnode.vertices]
        #log.debug(f"Convex verts bounds X: {min(v[0] for v in sourceverts)}, {max(v[0] for v in sourceverts)}")

        m = bpy.data.meshes.new(collisionnode.blockname)
        bm = bmesh.new()
        m.from_pydata(sourceverts, [], [])
        bm.from_mesh(m)

        bmesh.ops.convex_hull(bm, input=bm.verts)
        bm.to_mesh(m)

        obj = bpy.data.objects.new(collisionnode.blockname, m)
        bpy.context.view_layer.active_layer_collection.collection.objects.link(obj)
        
        try:
            obj['bhkMaterial'] = SkyrimHavokMaterial.get_name(prop.bhkMaterial)
        except:
            self.add_warning(f"Unknown havok material: {prop.bhkMaterial}")
            obj['bhkMaterial'] = str(prop.bhkMaterial)
        obj['bhkRadius'] = prop.bhkRadius * self.scale

        #log.info(f"1. Imported bhkConvexVerticesShape {obj.name} matrix: \n{obj.matrix_world}")
        if log.getEffectiveLevel() == logging.DEBUG:
            self.show_collision_normals(collisionnode, obj)
        obj.rotation_mode = "QUATERNION"
        q = collisionbody.rotation_quaternion.copy()
        q.invert()
        obj.rotation_quaternion = q
        #log.info(f"2. Imported bhkConvexVerticesShape {obj.name} matrix: \n{obj.matrix_world}")
        return obj


    def import_collision_shape(self, cs:CollisionShape, cb:bpy_types.Object):
        sh = None
        #log.debug(f"Found collision shape {cs.blockname}")
        if cs.blockname == "bhkBoxShape":
            sh = self.import_bhkBoxShape(cs, cb)
        elif cs.blockname == "bhkConvexVerticesShape":
            sh = self.import_bhkConvexVerticesShape(cs, cb)
        elif cs.blockname == "bhkListShape":
            sh = self.import_bhkListShape(cs, cb)
        elif cs.blockname == "bhkConvexTransformShape":
            sh = self.import_bhkConvexTransformShape(cs, cb)
        elif cs.blockname == "bhkCapsuleShape":
            sh = self.import_bhkCapsuleShape(cs, cb)
        else:
            self.add_warning(f"Found unimplemented collision shape: {cs.blockname}")
        
        if sh:
            sh.name = cs.blockname
            sh.parent = cb
            sh.color = COLLISION_COLOR


    collision_body_ignore = ['rotation', 'translation', 'guard', 'unusedByte1', 
                             'unusedInts1_0', 'unusedInts1_1', 'unusedInts1_2',
                             'unusedBytes2_0', 'unusedBytes2_1', 'unusedBytes2_2']

    def import_collision_body(self, cb:CollisionBody, c:bpy_types.Object):
        """Import the RigidBody node--c = its parent collision object."""
        bpy.ops.object.add(radius=self.scale, type='EMPTY')
        cbody = bpy.context.object
        cbody.matrix_world = Matrix() # Set to identity; will be reset if this is a bhkRigidBodyT
        cbody.parent = c
        cbody.name = cb.blockname
        cbody.show_name = True
        self.incr_loc
        #log.debug(f"Made collision body {cb.blockname} at {cbody.location}")

        p = cb.properties
        p.extract(cbody, ignore=self.collision_body_ignore)

        # The rotation in the nif is a quaternion with the angle in the 4th position, in radians
        # #log.debug(f"Found collision body with properties:\n{p}")
        if cb.blockname == "bhkRigidBodyT":
            cbody.rotation_mode = 'QUATERNION'
            #log.debug(f"Rotating collision body around quaternion {(p.rotation[3], p.rotation[0], p.rotation[1], p.rotation[2])}")
            cbody.rotation_quaternion = (p.rotation[3], p.rotation[0], p.rotation[1], p.rotation[2], )
            cbody.location = Vector(p.translation[0:3]) * HAVOC_SCALE_FACTOR * self.scale

        cs = cb.shape
        if cs:
            self.import_collision_shape(cs, cbody)


    def import_collision_obj(self, c:CollisionObject, parentObj=None, bone=None):
        """Import collision object. Parent is target of collision. If target is a bone,
        parent is armature and "bone" is bone name. Returns new collision object.
        """
        #log.debug(f"<import_collision_obj> for {parentObj}")
        col = None
        bpy.ops.object.mode_set(mode='OBJECT')
        if c.blockname == "bhkCollisionObject":
            bpy.ops.object.add(radius=self.scale, type='EMPTY')
            col = bpy.context.object
            col.matrix_world = Matrix()
            col.name = c.blockname
            col.show_name = True
            col['pynCollisionFlags'] = bhkCOFlags(c.flags).fullname

            if parentObj:
                col.parent = parentObj
                if parentObj.type == "ARMATURE":
                    col.matrix_world = self.calc_obj_transform(bone, scale_factor=self.scale)
                    col['pynCollisionTarget'] = bone.name

            cb = c.body
            if cb:
                self.import_collision_body(cb, col)
        return col

    def import_collisions(self):
        """Import top-level collision, if any """
        try:
            r = self.nif.rootNode
            if r.collision_object:
                self.import_collision_obj(r.collision_object, None)
        except:
            traceback.print_exc()
            self.add_warning(f"Cannot read collisions--collisions not imported")

    # ----- End Collisions ----


    # ----- Begin Animations ----

    def import_interpolator(self, ti:NiTransformInterpolator, target_node:bpy.types.Object, 
                            action:bpy.types.Action):
        """Import an interpolator, including its data block."""
        td = ti.data
        if td.properties.rotationType != NiKeyType.XYZ_ROTATION_KEY:
            self.add_warning(f"Nif contains unimplemented rotation type: {td.properties.rotationType}")
            return
        
        # The curve is all the keyframes for this one property.
        fps = bpy.context.scene.render.fps
        group_name = "Object Transforms"
        curveX = action.fcurves.new("rotation_euler", index=0, action_group=group_name)
        curveY = action.fcurves.new("rotation_euler", index=1, action_group=group_name)
        curveZ = action.fcurves.new("rotation_euler", index=2, action_group=group_name)

        for i, k in enumerate(td.animation_keys):
            newkeyX = curveX.keyframe_points.insert(k[0].properties.time * fps + 1, k[0].properties.value)
            newkeyY = curveY.keyframe_points.insert(k[1].properties.time * fps + 1, k[1].properties.value)
            newkeyZ = curveZ.keyframe_points.insert(k[2].properties.time * fps + 1, k[2].properties.value)


    def import_controlled_block(self, seq:NiSequence, block:ControllerLink):
        """Import one controlled block."""
        if block.controller_type != "NiTransformController":
            self.add_warning(f"Nif has unknown controller type: {block.controller_type}")
            return
        
        if block.node_name not in self.nif.nodes:
            self.add_warning(f"Controller target not found in nif: {block.node_name}")
            return
        target_node = self.nif.nodes[block.node_name]

        if target_node._handle in self.objects_created:
            target_obj = self.objects_created[target_node._handle]
        else:
            self.add_warning(f"Target object was not imported: {block.node_name}")
            return

        fps = bpy.context.scene.render.fps
        if not target_obj.animation_data:
            target_obj.animation_data_create()
        ad = target_obj.animation_data
        action_name = f"{block.node_name}_{seq.name}"
        ad.action = bpy.data.actions.new(action_name)
        ad.action.frame_start = seq.properties.startTime * fps + 1
        ad.action.frame_end = seq.properties.stopTime * fps + 1
        ad.action.use_frame_range = True

        self.import_interpolator(block.interpolator, target_obj, ad.action)


    def import_sequences(self, seq):
        """Import a single controller sequence."""
        for cb in seq.controlled_blocks:
            self.import_controlled_block(seq, cb)
        

    def import_animations(self):
        """Import all top-level animations."""
        for cm in self.nif.controller_managers:
            for seq in cm.controller_manager_seqs.values():
                self.import_sequences(seq)


    # ----- End Animations ----


    def import_nif(self):
        """Perform the import operation as previously defined."""
        log.info(f"Importing {self.nif.game} file {self.nif.filepath}")

        # Import shapes
        for s in self.nif.shapes:
            if self.nif.game in ['FO4', 'FO76'] and is_facebones(s.bone_names):
                self.nif.dict = fo4FaceDict
            self.nif.dict.use_niftools = self.rename_bones_nift
            self.import_shape(s)

        #log.debug("Linking objects to collections")
        for obj in self.loaded_meshes:
            if not obj.name in self.collection.objects:
                self.collection.objects.link(obj)

        if not self.mesh_only:
            # Import armature
            orphan_shapes = set(self.objects_created.values())
            if len(self.nif.shapes) == 0:
                log.info(f"No shapes in nif, importing bones as skeleton")
                if not self.armature:
                    self.armature = self.make_armature(self.collection)
                self.add_bones_to_arma(self.armature, self.nif, self.nif.nodes.keys())
                self.imported_armatures.append(self.armature)
                self.connect_armature(self.armature)
            else:
                # List of armatures available for shapes
                if self.armature:
                    self.imported_armatures = [self.armature] 

                if self.apply_skinning:
                    for obj in self.loaded_meshes:
                        sh = self.nodes_loaded[obj.name]
                        if sh.has_skin_instance:
                            target_arma, target_xf = self.find_compatible_arma(obj, self.imported_armatures)
                            self.armature = target_arma
                            new_arma = self.set_parent_arma(target_arma, obj, sh, target_xf) #target_xf)
                            if not target_arma:
                                self.imported_armatures.append(new_arma)
                                self.armature = new_arma
                            orphan_shapes.remove(obj)
                #log.debug("Connecting armature")
                for arma in self.imported_armatures:
                    if self.create_bones:
                        self.add_bones_to_arma(arma, self.nif, self.nif.nodes.keys())
                    self.connect_armature(arma)
                    if self.roll_bones_nift: self.roll_bones(arma)
    
            # Gather up any NiNodes that weren't captured any other way 
            self.import_loose_ninodes(self.nif)

            # Import nif-level extra data
            self.import_extra(self.nif)
        
            # Import top-level collisions
            self.import_collisions()

            # Import top-level animations
            self.import_animations()

            # Cleanup. Select everything and parent everything to the child connect point if any.
            objlist = [x for x in self.objects_created.values()]
            if objlist: 
                ObjectSelect(objlist)
                ObjectActive(objlist[0])

            for o in self.objects_created.values(): 
                if self.created_child_cp and o.parent == None and o != self.created_child_cp:
                    o.parent = self.created_child_cp


    def merge_shapes(self, filename, obj_list, new_filename, new_obj_list):
        """Merge new_obj_list into obj_list as shape keys
           If filenames follow PyNifly's naming conventions, create a shape key for the 
           base shape and rename the shape keys appropriately
        """
        # Can name shape keys to our convention if they end with underscore-something and everything
        # before the underscore is the same
        fn_parts = filename.split('_')
        new_fn_parts = new_filename.split('_')
        rename_keys = len(fn_parts) > 1 and len(new_fn_parts) > 1 and fn_parts[0:-1] == new_fn_parts[0:-1]
        obj_shape_name = '_' + fn_parts[-1]

        for obj, newobj in zip(obj_list, new_obj_list):
            ObjectSelect([obj, newobj])
            ObjectActive(obj)

            if rename_keys:
                if (not obj.data.shape_keys) or (not obj.data.shape_keys.key_blocks) \
                        or (obj_shape_name not in [s.name for s in obj.data.shape_keys.key_blocks]):
                    if not obj.data.shape_keys:
                        obj.shape_key_add(name='Basis')
                    obj.shape_key_add(name=obj_shape_name)

            bpy.ops.object.join_shapes()
            bpy.data.objects.remove(newobj)

            if rename_keys:
                obj.data.shape_keys.key_blocks[-1].name = '_' + new_fn_parts[-1]


    def connect_children_parents(self, parent_shapes, child_shapes):
        """If any of the child connect points in dictionary child_shapes should connect to the
        parent connect points in dictionary parent_shapes, parent them up
        """
        for connectname, parent in parent_shapes.items():
            # Find children that should connect to this parent. Could be more than one. 
            # Also the same child may be in the dictionary more than once under different
            # spellings of the name.
            try: 
                child = child_shapes[connectname]
                if not child.parent:
                    child.parent = parent
            except:
                pass


    def execute(self):
        """Perform the import operation as previously defined"""
        NifFile.clear_log()

        # All nif files imported into one collection 
        self.collection = bpy.data.collections.new(os.path.basename(self.filename))
        bpy.context.scene.collection.children.link(self.collection)
        bpy.context.view_layer.active_layer_collection \
             = bpy.context.view_layer.layer_collection.children[self.collection.name]
    
        self.loaded_parent_cp = {}
        self.loaded_child_cp = {}
        prior_vertcounts = []
        prior_fn = ''

        # Only use the active object if it's selected. Too confusing otherwise.
        if bpy.context.object and bpy.context.object.select_get():
            if bpy.context.object.type == "ARMATURE":
                self.armature = bpy.context.object
                log.info(f"Current object is an armature, parenting shapes to {self.armature.name}")
            elif bpy.context.object.type == "EMPTY" and bpy.context.object.name.startswith("BSConnectPointParents"):
                self.add_to_parents(bpy.context.object)
                log.info(f"Current object is a parent connect point, parenting shapes to {bpy.context.object.name}")
            elif bpy.context.object.type == 'MESH':
                prior_vertcounts = [len(bpy.context.object.data.vertices)]
                self.loaded_meshes = [bpy.context.object]
                log.info(f"Current object is a mesh, will import as shape key if possible: {bpy.context.object.name}")

        for this_file in self.filename_list:
            fn = os.path.splitext(os.path.basename(this_file))[0]

            self.nif = NifFile(this_file)
            if not self.reference_skel:
                self.reference_skel = self.nif.reference_skel

            prior_shapes = None
            this_vertcounts = [len(s.verts) for s in self.nif.shapes]
            if self.import_shapes:
                if len(this_vertcounts) > 0 and this_vertcounts == prior_vertcounts:
                    #log.debug(f"Vert count of all shapes in nif match shapes in prior nif. They will be loaded as a single shape with shape keys")
                    prior_shapes = self.loaded_meshes
            
            self.loaded_meshes = []
            self.mesh_only = (prior_shapes is not None)
            self.import_nif()

            if prior_shapes:
                ##log.debug(f"Merging shapes: {[s.name for s in prior_shapes]} << {[s.name for s in self.loaded_meshes]}")
                self.merge_shapes(prior_fn, prior_shapes, fn, self.loaded_meshes)
                self.loaded_meshes = prior_shapes
            else:
                prior_vertcounts = this_vertcounts
                prior_fn = fn

        # Connect up all the children loaded in this batch with all the parents loaded in this batch
        self.connect_children_parents(self.loaded_parent_cp, self.loaded_child_cp)


    @classmethod
    def do_import(cls, filename, chargen="chargen", scale=1.0):
        imp = NifImporter(filename, chargen=chargen, scale=scale)
        imp.execute()
        return imp


class ImportNIF(bpy.types.Operator, ImportHelper):
    """Load a NIF File"""
    bl_idname = "import_scene.pynifly"
    bl_label = "Import NIF (Nifly)"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".nif"
    filter_glob: StringProperty(
        default="*.nif",
        options={'HIDDEN'},
    )

    files: CollectionProperty(
        type=bpy.types.OperatorFileListElement,
        options={'HIDDEN', 'SKIP_SAVE'},)

    create_bones: bpy.props.BoolProperty(
        name="Create bones",
        description="Create vanilla bones as needed to make skeleton complete.",
        default=CREATE_BONES_DEF)

    scale_factor: bpy.props.FloatProperty(
        name="Scale correction",
        description="Scale import - set to 0.1 to match NifTools default",
        default=SCALE_DEF)

    rename_bones: bpy.props.BoolProperty(
        name="Rename bones",
        description="Rename bones to conform to Blender's left/right conventions.",
        default=RENAME_BONES_DEF)

    roll_bones: bpy.props.BoolProperty(
        name="Add bone roll",
        description="Add bone roll to work with animations.",
        default=ROLL_BONES_NIFT_DEF)

    rename_bones_niftools: bpy.props.BoolProperty(
        name="Rename bones as per NifTools",
        description="Rename bones using NifTools' naming scheme to conform to Blender's left/right conventions.",
        default=RENAME_BONES_NIFT_DEF)

    import_shapes: bpy.props.BoolProperty(
        name="Import as shape keys",
        description="Import similar objects as shape keys where possible on multi-file imports.",
        default=IMPORT_SHAPES_DEF)

    apply_skinning: bpy.props.BoolProperty(
        name="Apply skin to mesh",
        description="Applies any transforms defined in shapes' partitions to the final mesh.",
        default=APPLY_SKINNING_DEF)

    reference_skel: bpy.props.StringProperty(
        name="Reference skeleton",
        description="Reference skeleton to use for the bone hierarchy",
        default="")


    def execute(self, context):
        LogStart(bl_info, "IMPORT", "NIF")
        status = {'FINISHED'}

        #log.debug(f"Filepaths are {[f.name for f in self.files]}")
        #log.debug(f"Filepath is {self.filepath}")

        fullfiles = ''
        try:
            NifFile.Load(nifly_path)

            # bpy.ops.object.select_all(action='DESELECT')

            folderpath = os.path.dirname(self.filepath)
            filenames = [f.name for f in self.files]
            if len(filenames) > 0:
                fullfiles = [os.path.join(folderpath, f.name) for f in self.files]
            else:
                fullfiles = [self.filepath]
            imp = NifImporter(fullfiles, chargen=CHARGEN_EXT_DEF, scale=self.scale_factor)
            imp.create_bones = self.create_bones
            imp.roll_bones_nift = self.roll_bones
            imp.rename_bones = self.rename_bones
            imp.rename_bones_nift = self.rename_bones_niftools
            imp.import_shapes = self.import_shapes
            imp.apply_skinning = self.apply_skinning
            if self.reference_skel:
                imp.reference_skel = NifFile(self.reference_skel)
            imp.execute()
        
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    ctx = bpy.context.copy()
                    ctx['area'] = area
                    ctx['region'] = area.regions[-1]
                    bpy.ops.view3d.view_selected(ctx)

            status = set()
            for w in imp.warnings:
                #log.debug(f"Message is {w}")
                status.add(w[0])
                self.report({w[0]}, w[1])

            LogFinish("IMPORT", fullfiles, status, False)

        except:
            log.exception("Import of nif failed")
            self.report({"ERROR"}, "Import of nif failed, see console window for details")
            status = {'CANCELLED'}
            LogFinish("IMPORT", fullfiles, status, True)
                
        return {'FINISHED'}


# ### ---------------------------- TRI Files -------------------------------- ###

def create_shape_keys(obj, tri: TriFile):
    """Adds the shape keys in tri to obj 
        """
    mesh = obj.data
    if mesh.shape_keys is None:
        #log.debug(f"Adding first shape key to {obj.name}")
        newsk = obj.shape_key_add()
        mesh.shape_keys.use_relative=True
        newsk.name = "Basis"
        mesh.update()

    base_verts = tri.vertices

    dict = None
    if obj.parent and obj.parent.type == 'ARMATURE':
        g = best_game_fit(obj.parent.data.bones)
        if g != "":
            dict = gameSkeletons[g]

    for game_morph_name, morph_verts in sorted(tri.morphs.items()):
        if dict and game_morph_name in dict.morph_dic_blender:
            morph_name = dict.morph_dic_blender[game_morph_name]
        else:
            morph_name = game_morph_name
        if morph_name not in mesh.shape_keys.key_blocks:
            newsk = obj.shape_key_add()
            newsk.name = morph_name

            obj.active_shape_key_index = len(mesh.shape_keys.key_blocks) - 1
            #This is a pointer, not a copy
            mesh_key_verts = mesh.shape_keys.key_blocks[obj.active_shape_key_index].data
            # We may be applying the morphs to a different shape than the one stored in 
            # the tri file. But the morphs in the tri file are absolute locations, as are 
            # shape key locations. So we need to calculate the offset in the tri and apply that 
            # to our shape keys.
            for key_vert, morph_vert, base_vert in zip(mesh_key_verts, morph_verts, base_verts):
                key_vert.co[0] += morph_vert[0] - base_vert[0]
                key_vert.co[1] += morph_vert[1] - base_vert[1]
                key_vert.co[2] += morph_vert[2] - base_vert[2]
        
            mesh.update()

def create_trip_shape_keys(obj, trip:TripFile):
    """Adds the shape keys in trip to obj 
        """
    mesh = obj.data
    verts = mesh.vertices

    if mesh.shape_keys is None or "Basis" not in mesh.shape_keys.key_blocks:
        newsk = obj.shape_key_add()
        newsk.name = "Basis"

    offsetmorphs = trip.shapes[obj.name]
    for morph_name, morph_verts in sorted(offsetmorphs.items()):
        newsk = obj.shape_key_add()
        newsk.name = ">" + morph_name

        obj.active_shape_key_index = len(mesh.shape_keys.key_blocks) - 1
        #This is a pointer, not a copy
        mesh_key_verts = mesh.shape_keys.key_blocks[obj.active_shape_key_index].data
        for vert_index, offsets in morph_verts:
            for i in range(3):
                mesh_key_verts[vert_index].co[i] = verts[vert_index].co[i] + offsets[i]
        
        mesh.update()


def import_trip(filepath, target_objs):
    """Import a BS Tri file. 
       These TRI files do not have full shape data so they have to be matched to one of the 
       objects in target_objs.
       return = (set of result types: NOT_TRIP or WARNING. Null result means success,
                 list of shape names found in trip file)
       """
    result = set()
    shapelist = []
    trip = TripFile.from_file(filepath)
    if trip.is_valid:
        shapelist = trip.shapes.keys()
        for shapename, offsetmorphs in trip.shapes.items():
            matchlist = [o for o in target_objs if o.name == shapename]
            if len(matchlist) == 0:
                log.warning(f"BS Tri file shape does not match any selected object: {shapename}")
                result.add('WARNING')
            else:
                create_trip_shape_keys(matchlist[0], trip)
    else:
        result.add('NOT_TRIP')

    return (result, shapelist)


def import_tri(filepath, cobj):
    """Import the tris from filepath into cobj
       If cobj is None or if the verts don't match, create a new object
       """
    tri = TriFile.from_file(filepath)
    if not type(tri) == TriFile:
        log.error(f"Error reading tri file")
        return None

    new_object = None

    # Check whether selected object should receive shape keys
    if cobj and cobj.type == "MESH" and len(cobj.data.vertices) == len(tri.vertices):
        new_object = cobj
        new_mesh = new_object.data
        log.info(f"Verts match, loading tri into existing shape {new_object.name}")

    if new_object is None:
        new_mesh = bpy.data.meshes.new(os.path.basename(filepath))
        new_mesh.from_pydata(tri.vertices, [], tri.faces)
        new_object = bpy.data.objects.new(new_mesh.name, new_mesh)

        for f in new_mesh.polygons:
            f.use_smooth = True

        new_mesh.update(calc_edges=True, calc_edges_loose=True)
        new_mesh.validate(verbose=True)

        if tri.import_uv:
            mesh_create_uv(new_mesh, tri.uv_pos)
   
        new_collection = bpy.data.collections.new(os.path.basename(os.path.basename(filepath) + ".Coll"))
        bpy.context.scene.collection.children.link(new_collection)
        new_collection.objects.link(new_object)
        ObjectActive(new_object)
        ObjectSelect([new_object])

    create_shape_keys(new_object, tri)
    new_object.active_shape_key_index = 0

    return new_object


class ImportTRI(bpy.types.Operator, ImportHelper):
    """Load a TRI File"""
    bl_idname = "import_scene.pyniflytri"
    bl_label = "Import TRI (Nifly)"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".tri"
    filter_glob: StringProperty(
        default="*.tri",
        options={'HIDDEN'},
    )

    def execute(self, context):
        LogStart(bl_info, "IMPORT", "TRI")
        status = {'FINISHED'}

        try:
            
            imp = "IMPORT TRIP"
            v, s = import_trip(self.filepath, context.selected_objects)
            if 'NOT_TRIP' in v:
                imp = "IMPORT TRI"
                cobj = bpy.context.object
                obj = import_tri(self.filepath, cobj)
                if obj == cobj:
                    imp = f"IMPORT TRI into {cobj.name}"
                else:
                    imp = "IMPORT TRI as new object"
            else:
                # Have a TRIP file
                imp = f"IMPORT TRIP {list(s)}"
            status = status.union(v)
            #log.debug(f"Imported tri/trip, got status {status}")
        
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    ctx = bpy.context.copy()
                    ctx['area'] = area
                    ctx['region'] = area.regions[-1]
                    bpy.ops.view3d.view_selected(ctx)

            LogFinish(imp, self.filepath, status, False)
            if 'WARNING' in status:
                self.report({"ERROR"}, "Import completed with warnings, see console for details")

        except:
            log.exception("Import of tri failed")
            self.report({"ERROR"}, "Import of tri failed, see console window for details")
            status = {'CANCELLED'}
            LogFinish(imp, self.filepath, status, True)
        
        return status.intersection({'FINISHED', 'CANCELLED'})

# ### ---------------------------- EXPORT -------------------------------- ###

def clean_filename(fn):
    return "".join(c for c in fn.strip() if (c.isalnum() or c in "._- "))

def select_all_faces(mesh):
    """ Make sure all mesh elements are visible and all faces are selected """
    bpy.ops.object.mode_set(mode = 'OBJECT') # Have to be in object mode

    for v in mesh.vertices:
        v.hide = False
    for e in mesh.edges:
        e.hide = False
    for p in mesh.polygons:
        p.hide = False
        p.select = True


def check_partitions(vi1, vi2, vi3, weights):
    """ Chcek whether the = 3 verts (specified by index) all have the same partitions 
        weights = [dict[group-name: weight], ...] vertex weights, 1:1 with verts
       """
    p1 = set([k for k in weights[vi1].keys() if is_partition(k)])
    p2 = set([k for k in weights[vi2].keys() if is_partition(k)])
    p3 = set([k for k in weights[vi3].keys() if is_partition(k)])
    #log.debug(f"Checking tri: {p1}, {p2}, {p3}")
    return len(p1.intersection(p2, p3)) > 0


def trim_to_four(weights, arma):
    """ Trim to the 4 heaviest weights in the armature
        weights = [(group_name: weight), ...] """
    if arma:
        ##log.debug(f"Trimming to 4 on armature {arma.name}")
        lst = filter(lambda p: p[0] in arma.data.bones, weights)
        ##log.debug(f"Arma weights: {lst}")
        notlst = filter(lambda p: p[0] not in arma.data.bones, weights)
        sd = sorted(lst, reverse=True, key=lambda item: item[1])[0:4]
        ##log.debug(f"Arma weights sorted: {sd}")
        sd.extend(notlst)
        #if len(sd) != len(weights):
        #    log.info(f"Trimmed weights to {sd}")
        return dict(sd)
    else:
        return dict(weights)


def has_uniform_scale(obj):
    """ Determine whether an object has uniform scale """
    return NearEqual(obj.scale[0], obj.scale[1]) and NearEqual(obj.scale[1], obj.scale[2])


def extract_vert_info(obj, mesh, arma, target_key='', scale_factor=1.0):
    """Returns 3 lists of equal length with one entry each for each vertex
    *   verts = [(x, y, z)... ] - base or as modified by target-key if provided
    *   weights = [{group-name: weight}... ] - 1:1 with verts list
    *   dict = {shape-key: [verts...], ...} - verts list for each shape which is valid for export.
            shape-key is the blender name.
        """
    weights = []
    morphdict = {}
    msk = mesh.shape_keys

    sf = Vector((1,1,1))
    if not has_uniform_scale(obj):
        # Apply non-uniform scale to verts directly
        sf = obj.scale

    if target_key != '' and msk and target_key in msk.key_blocks.keys():
        #log.debug(f"....exporting shape {target_key} only")
        verts = [(v.co * sf / scale_factor)[:] for v in msk.key_blocks[target_key].data]
    else:
        verts = [(v.co * sf / scale_factor)[:] for v in mesh.vertices]
    ##log.debug(f"extract_vert_info max z is {max([v[2] for v in verts])}")

    for i, v in enumerate(mesh.vertices):
        vert_weights = []
        for vg in v.groups:
            try:
                vgn = obj.vertex_groups[vg.group].name
                vert_weights.append([vgn, vg.weight])
            except:
                log.error(f"ERROR: Vertex #{v.index} references invalid group #{vg.group}")
        
        weights.append(trim_to_four(vert_weights, arma))
    
    if msk: # and target_key == '' 
        for sk in msk.key_blocks:
            morphdict[sk.name] = [(v.co * sf)[:] for v in sk.data]

    return verts, weights, morphdict


def tag_unweighted(obj, bones):
    """ Find and return verts that are not weighted to any of the given bones 
        result = (v_index, ...) list of indices into the vertex list
    """
    #log.debug(f"..Checking for unweighted verts on {obj.name}")
    unweighted_verts = []
    for v in obj.data.vertices:
        maxweight = 0.0
        if len(v.groups) > 0:
            maxweight = max([g.weight for g in v.groups])
        if maxweight < 0.0001:
            unweighted_verts.append(v.index)
    #log.debug(f"..Unweighted vert count: {len(unweighted_verts)}")
    return unweighted_verts


def create_group_from_verts(obj, name, verts):
    """ Create a vertex group from the list of vertex indices.
    Use the existing group if any """
    if name in obj.vertex_groups.keys():
        g = obj.vertex_groups[name]
    else:
        g = obj.vertex_groups.new(name=name)
    g.add(verts, 1.0, 'ADD')


def best_game_fit(bonelist):
    """ Find the game that best matches the skeleton """
    boneset = set([b.name for b in bonelist])
    maxmatch = 0
    matchgame = ""
    #print(f"Checking bonelist {[b.name for b in bonelist]}")
    for g, s in gameSkeletons.items():
        n = s.matches(boneset)
        if n > maxmatch:
            maxmatch = n
            matchgame = g
    n = fo4FaceDict.matches(boneset)
    if n > maxmatch:
        matchgame = "FO4"
    return matchgame


def expected_game(nif, bonelist):
    """ Check whether the nif's game is the best match for the given bonelist """
    matchgame = best_game_fit(bonelist)
    return matchgame == "" or matchgame == nif.game or \
        (matchgame in ['SKYRIM', 'SKYRIMSE'] and nif.game in ['SKYRIM', 'SKYRIMSE'])


def is_partition(name):
    """ Check whether <name> is a valid partition or segment name """
    if SkyPartition.name_match(name) >= 0:
        return True

    if FO4Segment.name_match(name) >= 0:
        return True

    parent_name, subseg_id, material = FO4Subsegment.name_match(name)
    if parent_name:
        return True

    return False


def partitions_from_vert_groups(obj):
    """ Return dictionary of Partition objects for all vertex groups that match the partition 
        name pattern. These are all partition objects including subsegments.
    """
    val = {}
    if obj.vertex_groups:
        vg_sorted = sorted([g.name for g in obj.vertex_groups])
        for nm in vg_sorted:
            vg = obj.vertex_groups[nm]
            skyid = SkyPartition.name_match(vg.name)
            if skyid >= 0:
                val[vg.name] = SkyPartition(part_id=skyid, flags=0, name=vg.name)
            else:
                segid = FO4Segment.name_match(vg.name)
                if segid >= 0:
                    ##log.debug(f"Found FO4Segment '{vg.name}'")
                    val[vg.name] = FO4Segment(part_id=len(val), index=segid, name=vg.name)
                else:
                    # Check if this is a subsegment. All segs sort before their subsegs, 
                    # so it will already have been created if it exists separately
                    parent_name, subseg_id, material = FO4Subsegment.name_match(vg.name)
                    if parent_name:
                        if not parent_name in val:
                            # Create parent segments if not there
                            #log.debug(f"Subseg {vg.name} needs parent {parent_name}; existing parents are {val.keys()}")
                            val[parent_name] = FO4Segment(len(val), 0, parent_name)
                        p = val[parent_name]
                        ##log.debug(f"Found FO4Subsegment '{vg.name}' child of '{parent_name}'")
                        val[vg.name] = FO4Subsegment(len(val), subseg_id, material, p, name=vg.name)
    
    return val


def all_vertex_groups(weightdict):
    """ Return the set of group names that have non-zero weights """
    val = set()
    for g, w in weightdict.items():
        if w > 0.0001:
            val.add(g)
    return val


def get_loop_color(mesh, loopindex, cm, am):
    """ Return the color of the vertex-in-loop at given loop index using
        cm = color map to use
        am = alpha map to use """
    vc = mesh.vertex_colors
    alpha = 1.0
    color = (1.0, 1.0, 1.0)
    if cm:
        #log.debug( f"Loop index less than color length {loopindex} < {len(cm)}")
        color = cm[loopindex].color
    if am:
        #log.debug(f"Loop index less than alpha length {loopindex} < {len(am)}")
        acolor = am[loopindex].color
        alpha = (acolor[0] + acolor[1] + acolor[2])/3

    return (color[0], color[1], color[2], alpha)
    

def mesh_from_key(editmesh, verts, target_key):
    faces = []
    for p in editmesh.polygons:
        faces.append([editmesh.loops[lpi].vertex_index for lpi in p.loop_indices])
    newverts = [v.co[:] for v in editmesh.shape_keys.key_blocks[target_key].data]
    newmesh = bpy.data.meshes.new(editmesh.name)
    newmesh.from_pydata(newverts, [], faces)
    return newmesh


def get_common_shapes(obj_list) -> set:
    """Return the shape keys found in any of the given objects """
    res = None
    for obj in obj_list:
        o_shapes = set()
        if obj.data.shape_keys:
            o_shapes = set(obj.data.shape_keys.key_blocks.keys())
        if res:
            res = res.union(o_shapes)
        else:
            res = o_shapes
    if res:
        res = list(res)
    return res


def get_with_uscore(str_list):
    if str_list:
        return list(filter((lambda x: x[0] == '_'), str_list))
    else:
        return []


class NifExporter:
    """ Object that handles the export process independent of Blender's export class """
    def __init__(self, filepath, game, export_flags=pynFlags.RENAME_BONES, chargen="chargen", scale=1.0):
        self.filepath = filepath
        self.game = game
        self.nif = None
        self.trip = None
        self.warnings = set()
        self.armature = None
        self.facebones = None
        self.rename_bones = RENAME_BONES_DEF
        self.rename_bones_nift = RENAME_BONES_NIFT_DEF
        self.preserve_hierarchy = PRESERVE_HIERARCHY_DEF
        self.write_bodytri = WRITE_BODYTRI_DEF
        self.export_pose = EXPORT_POSE_DEF
        self.export_modifiers = EXPORT_MODIFIERS_DEF
        self.active_obj = None
        self.scale = scale

        # Objects that are to be written out
        self.objects = [] # Ordered list of objects to write--first my have root node info
        self.bg_data = set()
        self.str_data = set()
        self.cloth_data = set()
        self.grouping_nodes = set()
        self.bsx_flag = None
        self.inv_marker = None
        self.furniture_markers = set()
        self.collisions = set()
        self.connect_parent = set()
        self.connect_child = set()
        self.trippath = ''
        self.chargen_ext = chargen
        self.writtenbones = {}
        
        # Shape keys that start with underscore trigger a separate file export
        # for each shape key
        self.file_keys = []  
        self.objs_unweighted = set()
        self.objs_scale = set()
        self.objs_mult_part = set()
        self.objs_no_part = set()
        self.arma_game = []
        self.bodytri_written = False

        # Dictionary of objects written to nif. {Blender object name: NiNode}
        self.objs_written = {}

        self.message_log = []
        #self.rotate_model = rotate


    def __str__(self):
        flags = []
        if self.rename_bones: flags.append("RENAME_BONES")
        if self.rename_bones_nift: flags.append("RENAME_BONES_NIFT")
        if self.preserve_hierarchy: flags.append("PRESERVE_HIERARCHY")
        if self.write_bodytri: flags.append("WRITE_BODYTRI")
        if self.export_pose: flags.append("EXPORT_POSE")
        if self.export_modifiers: flags.append("EXPORT_MODIFIERS")
        return f"""
        Exporting objects: {self.objects}
            flags: {'|'.join(flags)}
            string data: {self.str_data}
            BG data: {self.bg_data}
            cloth data: {self.cloth_data}
            collisions: {self.collisions}
            armature: {self.armature.name if self.armature else 'None'}
            facebones: {self.facebones.name if self.facebones else 'None'}
            parent connect points: {self.connect_parent}
            child connect points: {self.connect_child}
            scale factor: {round(self.scale, 4)}
            shapes: {self.file_keys}
            to file: {self.filepath}
        """

    def nif_name(self, blender_name):
        if self.rename_bones or self.rename_bones_nift:
            return self.nif.nif_name(blender_name)
        else:
            return blender_name

    def export_shape_data(self, obj, shape):
        """ Export a shape's extra data """
        edlist = []
        strlist = []
        for ch in obj.children:
             if 'NiStringExtraData_Name' in ch:
                strlist.append( (ch['NiStringExtraData_Name'], ch['NiStringExtraData_Value']) )
                self.objs_written[ch.name] = shape
             if 'BSBehaviorGraphExtraData_Name' in ch:
                edlist.append( (ch['BSBehaviorGraphExtraData_Name'], 
                               ch['BSBehaviorGraphExtraData_Value']) )
                self.objs_written[ch.name] = shape
        #ed = [ (x['NiStringExtraData_Name'], x['NiStringExtraData_Value']) for x in \
        #        obj.children if 'NiStringExtraData_Name' in x.keys()]
        if len(strlist) > 0:
            shape.string_data = strlist
    
        #ed = [ (x['BSBehaviorGraphExtraData_Name'], x['BSBehaviorGraphExtraData_Value']) \
        #        for x in obj.children if 'BSBehaviorGraphExtraData_Name' in x.keys()]
        if len(edlist) > 0:
            shape.behavior_graph_data = edlist


    def add_armature(self, arma):
        """Add an armature to the export"""
        facebones_arma = (self.game in ['FO4', 'FO76']) and (is_facebones(arma.data.bones.keys()))
        if facebones_arma and self.facebones is None:
            self.facebones = arma
        if (not facebones_arma) and (self.armature is None):
            self.armature = arma 


    def add_object(self, obj):
        """Adds the given object to the objects to export. Object may be mesh, armature,
        or anything else. 
        
        * If an armature is selected, all child objects are exported 
        * If a skinned mesh is selected, all armatures referenced in armature modifiers
          are considered for export.
        """
        if obj.type == 'ARMATURE':
            self.add_armature(obj)

        elif obj.type == 'MESH':
            # Export the mesh, but use its parent and use any armature modifiers
            if obj not in self.objects: self.objects.append(obj)

        elif obj.type == 'EMPTY':
            if 'BSBehaviorGraphExtraData_Name' in obj.keys():
                self.bg_data.add(obj)

            elif 'NiStringExtraData_Name' in obj.keys():
                self.str_data.add(obj)

            elif 'BSClothExtraData_Name' in obj.keys():
                self.cloth_data.add(obj)

            elif 'BSXFlags_Name' in obj.keys():
                self.bsx_flag = obj

            elif 'BSInvMarker_Name' in obj.keys():
                self.inv_marker = obj

            elif obj.name.startswith("BSFurnitureMarkerNode"):
                self.furniture_markers.add(obj)

            elif obj.name.startswith("BSConnectPointParents"):
                self.connect_parent.add(obj)

            elif obj.name.startswith("BSConnectPointChildren"):
                self.connect_child.add(obj)

            elif obj.name.startswith("bhkCollisionObject"):
                self.collisions.add(obj)

            else:
                self.grouping_nodes.add(obj)
                for c in obj.children:
                    self.add_object(c)


    def set_objects(self, objects:list):
        """ Set the objects to export from the given list of objects 
        """
        #log.debug(f"<set_objects> {objects}")
        for x in objects:
            self.add_object(x)
        self.file_keys = get_with_uscore(get_common_shapes(self.objects))


    # def from_context(self, context):
    #     """ Set the objects to export from the given context. Ensures the active object is
    #     first."""
    #     objlist = []
    #     if context.object and context.object.select_get():
    #         objlist.append(context.object)
    #     for o in context.selected_objects:
    #         if o != context.object:
    #             objlist.append(o)
    #     self.set_objects(objlist)


    # --------- DO THE EXPORT ---------

    def export_tris(self, obj, verts, tris, uvs, morphdict):
        """ Export a tri file to go along with the given nif file, if there are shape keys 
            and it's not a faceBones nif.
            dict = {shape-key: [verts...], ...} - verts list for each shape which is valid for export.
        """
        result = {'FINISHED'}

        ##log.debug(f"export_tris called with {morphdict.keys()}")

        if obj.data.shape_keys is None or len(morphdict) == 0:
            return result

        fpath = os.path.split(self.nif.filepath)
        fname = os.path.splitext(fpath[1])

        if fname[0].endswith('_faceBones'):
            return result

        fname_tri = os.path.join(fpath[0], fname[0] + ".tri")
        fname_chargen = os.path.join(fpath[0], fname[0] + self.chargen_ext + ".tri")
        if self.chargen_ext != CHARGEN_EXT_DEF: obj['PYN_CHARGEN_EXT'] = self.chargen_ext 

        # Don't export anything that starts with an underscore or asterisk
        objkeys = obj.data.shape_keys.key_blocks.keys()
        export_keys = set(filter((lambda n: n[0] not in ('_', '*') and n != 'Basis'), objkeys))
        expression_morphs = self.nif.dict.expression_filter(export_keys)
        trip_morphs = set(filter((lambda n: n[0] == '>'), objkeys))
        # Leftovers are chargen candidates
        leftover_morphs = export_keys.difference(expression_morphs).difference(trip_morphs)
        chargen_morphs = self.nif.dict.chargen_filter(leftover_morphs)

        if len(expression_morphs) > 0 and len(trip_morphs) > 0:
            log.warning(f"Found both expression morphs and BS tri morphs in shape {obj.name}. May be an error.")
            result = {'WARNING'}

        if len(expression_morphs) > 0:
            tri = TriFile()
            tri.vertices = verts
            tri.faces = tris
            tri.uv_pos = uvs
            tri.face_uvs = tris # (because 1:1 with verts)
            for m in expression_morphs:
                if m in self.nif.dict.morph_dic_game:
                    triname = self.nif.dict.morph_dic_game[m]
                else:
                    triname = m
                if m in morphdict:
                    tri.morphs[triname] = morphdict[m]
    
            log.info(f"Generating tri file '{fname_tri}'")
            tri.write(fname_tri) # Only expression morphs to write at this point

        if len(chargen_morphs) > 0:
            tri = TriFile()
            tri.vertices = verts
            tri.faces = tris
            tri.uv_pos = uvs
            tri.face_uvs = tris # (because 1:1 with verts)
            for m in chargen_morphs:
                if m in morphdict:
                    tri.morphs[m] = morphdict[m]
    
            log.info(f"Generating tri file '{fname_chargen}'")
            tri.write(fname_chargen, chargen_morphs)

        if len(trip_morphs) > 0:
            expdict = {}
            for k, v in morphdict.items():
                if k[0] == '>':
                    n = k[1:]
                    expdict[n] = v
            self.trip.set_morphs(obj.name, expdict, verts)
            
        return result


    def export_extra_data(self):
        """ Export any top-level extra data represented as Blender objects. 
            Sets self.bodytri_done if one of the extra data nodes represents a bodytri
        """
        sdlist = []
        for st in self.str_data:
            if st['NiStringExtraData_Name'] != 'BODYTRI' or self.game not in ['FO4', 'FO76']:
                # FO4 bodytris go at the top level
                sdlist.append( (st['NiStringExtraData_Name'], st['NiStringExtraData_Value']) )
                self.objs_written[st.name] = self.nif
                self.bodytri_written |= (st['NiStringExtraData_Name'] == 'BODYTRI')

        if len(sdlist) > 0:
            self.nif.string_data = sdlist
        
        bglist = []
        for bg in self.bg_data: 
            bglist.append( (bg['BSBehaviorGraphExtraData_Name'], 
                            bg['BSBehaviorGraphExtraData_Value'], 
                            bg['BSBehaviorGraphExtraData_CBS']) )
            self.objs_written[bg.name] = self.nif

        if len(bglist) > 0:
            self.nif.behavior_graph_data = bglist 

        cdlist = []
        for cd in self.cloth_data:
            cdlist.append( (cd['BSClothExtraData_Name'], 
                            codecs.decode(cd['BSClothExtraData_Value'], "base64")) )
            self.objs_written[cd.name] = self.nif

        if len(cdlist) > 0:
            self.nif.cloth_data = cdlist 

        if self.bsx_flag:
            #log.debug(f"Exporting BSXFlags node")
            self.nif.bsx_flags = [self.bsx_flag['BSXFlags_Name'],
                                  BSXFlags.parse(self.bsx_flag['BSXFlags_Value'])]
            self.objs_written[self.bsx_flag.name] = self.nif

        if self.inv_marker:
            #log.debug(f"Exporting BSInvMarker node")
            self.nif.inventory_marker = [self.inv_marker['BSInvMarker_Name'], 
                                         self.inv_marker['BSInvMarker_RotX'], 
                                         self.inv_marker['BSInvMarker_RotY'], 
                                         self.inv_marker['BSInvMarker_RotZ'], 
                                         self.inv_marker['BSInvMarker_Zoom']]
            self.objs_written[self.inv_marker.name] = self.nif

        fmklist = []
        for fm in self.furniture_markers:
            buf = FurnitureMarkerBuf()
            buf.offset = (fm.location / self.scale)[:]
            buf.heading = fm.rotation_euler.z
            buf.animation_type = FurnAnimationType.GetValue(fm['AnimationType'])
            buf.entry_points = FurnEntryPoints.parse(fm['EntryPoints'])
            fmklist.append(buf)
        
        if fmklist:
            self.nif.furniture_markers = fmklist

        connect_par = []
        for cp in self.connect_parent:
            buf = ConnectPointBuf()
            buf.name = cp.name.split("::")[1].encode('utf-8')
            if cp.parent and cp.parent.type != 'ARMATURE':
                buf.parent = nonunique_name(cp.parent).encode('utf-8')
                buf.translation[0], buf.translation[1], buf.translation[2] \
                    = cp.matrix_world.translation[:]
                buf.rotation[0], buf.rotation[1], buf.rotation[2], buf.rotation[3] \
                    = cp.matrix_world.to_quaternion()[:]
                buf.scale = cp.matrix_world.to_scale()[0] / CONNECT_POINT_SCALE
            elif cp.parent and cp.parent.type == 'ARMATURE':
                parentname = ''
                if 'pynConnectParent' in cp:
                    parentname = cp['pynConnectParent']
                elif 'PYN_CONNECT_PARENT' in cp:
                    # Older representation of parent
                    parentname = cp['PYN_CONNECT_PARENT']
                buf.parent = parentname.encode('utf-8')
                parentnamebl = self.nif.dict.blender_name(parentname)
                if parentnamebl in cp.parent.data.bones:
                    parentbone = cp.parent.data.bones[parentnamebl]
                    log.debug(f"Connect point {cp.name} parent is bone {parentbone.name}")
                    log.debug(f"Connect point translation is {cp.matrix_world.translation}")
                    log.debug(f"Parent bone translation is {parentbone.matrix_local.translation}")
                    mx = parentbone.matrix_local.inverted() @ cp.matrix_world
                    log.debug(f"Have connect point translation {mx.translation}")
                    buf.translation[0] = mx.translation[0]
                    buf.translation[1] = mx.translation[1]
                    buf.translation[2] = mx.translation[2]
                    buf.rotation[0], buf.rotation[1], buf.rotation[2], buf.rotation[3] \
                        = mx.to_quaternion()[:]
                    buf.scale = mx.to_scale()[0] / CONNECT_POINT_SCALE
            
            log.debug(f"Writing parent connect point {cp.name} at {buf.translation[:]}")
            connect_par.append(buf)
        if connect_par:
            self.nif.connect_points_parent = connect_par

        child_names = []
        for cp in self.connect_child:
            self.nif.connect_pt_child_skinned = cp['PYN_CONNECT_CHILD_SKINNED']
            ##log.debug(f"Extending child names with {[cp[x] for x in cp.keys() if x != 'PYN_CONNECT_CHILD_SKINNED' and x.startswith('PYN_CONNECT_CHILD')]}")
            child_names.extend([cp[x] for x in cp.keys() if x != 'PYN_CONNECT_CHILD_SKINNED' and x.startswith('PYN_CONNECT_CHILD')])
        if child_names:
            ##log.debug(f"Writing connect point children: {child_names}")
            self.nif.connect_points_child = child_names


    def export_bhkCapsuleShape(self, s, xform):
        """Export capsule shape. 
        Returns (shape, coordinates)
        shape = collision shape in the nif object
        coordinates = center of the shape in Blender world coordinates) 
        """ 
        cshape = None
        center = Vector()

        # Capsule covers the extent of the shape
        props = bhkCapsuleShapeProps(s)
        xf = s.matrix_local
        xfv = [xf @ v.co for v in s.data.vertices]

        maxx = max([v[0] for v in xfv])
        maxy = max([v.y for v in xfv])
        maxz = max([v[2] for v in xfv])
        minx = min([v[0] for v in xfv])
        miny = min([v[1] for v in xfv])
        minz = min([v[2] for v in xfv])
        halfspanx = (maxx - minx)/2
        halfspany = (maxy - miny)/2
        halfspanz = (maxz - minz)/2
        center = s.matrix_world @ Vector([minx + halfspanx, miny + halfspany, minz + halfspanz])
        
        sf = HAVOC_SCALE_FACTOR * self.scale * game_collision_sf[self.game]
        props.bhkRadius = (halfspanx / sf) 
        props.radius1 = (halfspanx / sf) 
        props.radius2 = (halfspanx / sf) 

        props.point1[0] = ((minx+halfspanx) / sf) 
        props.point1[1] = (maxy / sf) 
        props.point1[2] = ((minz+halfspanz) / sf) 
        props.point2[0] = ((minx+halfspanx) / sf) 
        props.point2[1] = (miny / sf) 
        props.point2[2] = ((minz+halfspanz) / sf) 
        cshape = self.nif.add_coll_shape("bhkCapsuleShape", props)

        return cshape, center


    def export_bhkBoxShape(self, s, xform):
        """Export box shape. 
        Returns (shape, coordinates)
        shape = collision shape in the nif object
        coordinates = center of the shape in Blender world coordinates) 
        """ 
        cshape = None
        center = Vector()
        try:
            # Box covers the extent of the shape, whatever it is
            p = bhkBoxShapeProps(s)
            xfv = [v.co for v in s.data.vertices]
            maxx = max([v[0] for v in xfv])
            maxy = max([v[1] for v in xfv])
            maxz = max([v[2] for v in xfv])
            minx = min([v[0] for v in xfv])
            miny = min([v[1] for v in xfv])
            minz = min([v[2] for v in xfv])
            halfspanx = (maxx - minx)/2
            halfspany = (maxy - miny)/2
            halfspanz = (maxz - minz)/2
            center = s.matrix_world @ Vector([minx + halfspanx, miny + halfspany, minz + halfspanz])
                
            sf = HAVOC_SCALE_FACTOR * self.scale * game_collision_sf[self.game]
            p.bhkDimensions[0] = (halfspanx / sf) 
            p.bhkDimensions[1] = (halfspany / sf) 
            p.bhkDimensions[2] = (halfspanz / sf) 
            if 'radius' not in s.keys():
                p.bhkRadius = (max(halfspanx, halfspany, halfspanz) / sf) 
            cshape = self.nif.add_coll_shape("bhkBoxShape", p)
            #log.debug(f"Created collision shape with dimensions {p.bhkDimensions[:]}")
        except:
            log.exception(f"Cannot create collision shape from {s.name}")
            self.warnings.add('WARNING')

        return cshape, center
        

    def export_bhkConvexVerticesShape(self, s, xform):
        """Export a convex vertices shape that wraps around whatever the import shape is."""
        effectiveXF = xform @ s.matrix_local 

        p = bhkConvexVerticesShapeProps(s)
        bm = bmesh.new()
        bm.from_mesh(s.data)
        bmesh.ops.convex_hull(bm, input=bm.verts, use_existing_faces=True)

        verts1 = [effectiveXF @ v.co for v in bm.verts]
        # verts1 = [xform @ v.co for v in s.data.vertices]
        sf = HAVOC_SCALE_FACTOR * self.scale * game_collision_sf[self.nif.game]
        verts = [(v / sf) for v in verts1]

        # Need a normal for each face
        norms = []
        for face in s.data.polygons:
            # Length needs to be distance from origin to face along this normal
            facevert = s.data.vertices[face.vertices[0]].co
            vintersect = geometry.distance_point_to_plane(
                Vector((0,0,0)), facevert, face.normal)
            n = Vector((face.normal[0], face.normal[1], face.normal[2], vintersect/sf))
            append_if_new(norms, n, 0.1)
        
            cshape = self.nif.add_coll_shape("bhkConvexVerticesShape", p, verts, norms)

        return cshape, Vector()


    def export_bhkConvexTransformShape(self, s, xform):
        childxf = xform @ s.matrix_local
        childnode, childcenter = self.export_collision_shape(s.children, childxf)

        if not childnode:
            return None, None

        props = bhkConvexTransformShapeProps(s)
        props.bhkRadius = s["bhkRadius"] / self.scale
        havocxf = s.matrix_world.copy()
        sf = HAVOC_SCALE_FACTOR * self.scale * game_collision_sf[self.nif.game]
        havocxf.translation = havocxf.translation / sf
        cshape = self.nif.add_coll_shape("bhkConvexTransformShape", 
                                         props, transform=havocxf)
        #log.debug(f"Exporting bhkConvexTransformShape with material {props.F}")
        cshape.child = childnode
        return cshape, xform.translation


    def export_bhkListShape(self, s, xform):
        props = bhkListShapeProps(s)
        cshape = self.nif.add_coll_shape("bhkListShape", props)

        xf = s.matrix_local @ xform
        for ch in s.children: 
            if ch.name.startswith("bhk"):
                shapenode, nodetransl = self.export_collision_shape([ch], xf)
                if shapenode:
                    cshape.add_child(shapenode)

        return cshape, s.matrix_local.translation


    def export_collision_shape(self, shape_list, xform=Matrix()):
        """ Takes a list of shapes, but only exports the first one """
        for cs in shape_list:
            if cs.name.startswith("bhkBoxShape"):
                return self.export_bhkBoxShape(cs, xform)
            elif cs.name.startswith("bhkConvexVerticesShape"):
                return self.export_bhkConvexVerticesShape(cs, xform)
            elif cs.name.startswith("bhkListShape"):
                return self.export_bhkListShape(cs, xform)
            elif cs.name.startswith("bhkCapsuleShape"):
                return self.export_bhkCapsuleShape(cs, xform)
            elif cs.name.startswith("bhkConvexTransformShape"):
                return self.export_bhkConvexTransformShape(cs, xform)
        return None, None

    def get_collision_target(self, collisionobj) -> Matrix:
        """Return the world transform matrix for the collision target. If the target
        is the root node return None. """
        mx = None
        targ = collisionobj.parent
        if targ == None:
            mx = collisionobj.matrix_world.copy()
            log.exception(f"No target, using collision object: {collisionobj.name}")
            return mx

        if targ.type == 'ARMATURE':
            targname = collisionobj['pynCollisionTarget']
            #log.debug(f"Finding target bone: {targname}")
            mx = get_bone_xform(targ, targname, self.game, 
                                self.preserve_hierarchy,
                                self.export_pose)
            return mx

        mx = targ.matrix_world.copy()
        #log.debug(f"Using parent object: {targ.name}")
        return mx


    def export_collision_body(self, body_list, coll):
        """ Export the collision body elements. coll is the parent collision object """
        body = None
        for b in body_list:
            blockname = 'bhkRigidBody'
            if b.name.startswith('bhkRigidBodyT'):
                blockname = 'bhkRigidBodyT'

            targxf = self.get_collision_target(coll)

            xform = Matrix()
            if blockname == 'bhkRigidBody':
                # Get verts in world coords 
                xform = b.matrix_world.copy()
                # xform.invert()
                # Apply the transform from target
                targxfi = targxf.copy()
                targxfi.invert()
                xform = targxfi @ xform

            cshape, ctr = self.export_collision_shape(b.children, xform)
            ##log.debug(f"Collision Center: {ctr}")

            if cshape:
                # Coll body can be anywhere. What matters is the location of the collision
                # shape relative to the collision target--that gets stored on the
                # collision body
                props = bhkRigidBodyProps(b)
                
                # If there's no target, root is the target. We don't support transforms 
                # on root yet.
                targloc, targq, targscale = targxf.decompose()
            
                targq.invert()
                props.rotation[0] = targq.x
                props.rotation[1] = targq.y
                props.rotation[2] = targq.z
                props.rotation[3] = targq.w
                ##log.debug(f"Target rotation: {targq.w}, {targq.x}, {targq.y}, {targq.z}")

                rv = ctr - targloc
                ##log.debug(f"Target to center: {rv}")
                if blockname == 'bhkRigidBodyT':
                    rv.rotate(targq)

                sf = HAVOC_SCALE_FACTOR * self.scale 
                props.translation[0] = rv.x / sf
                props.translation[1] = rv.y / sf
                props.translation[2] = rv.z / sf
                props.translation[3] = 0

                body = self.nif.add_rigid_body(blockname, props, cshape)
        return body

    def export_collisions(self, objlist):
        """ Export all the collisions in objlist. (Should be only one.) Apply the skin first so bones are available. """
        #log.debug("Writing collisions")
        for coll in objlist:
            body = self.export_collision_body(coll.children, coll)
            if body:
                if coll.name not in self.objs_written:
                    targnode = None
                    p = coll.parent
                    if p == None:
                        targnode = self.nif.rootNode
                    elif p.type == "ARMATURE":
                        targname = coll['pynCollisionTarget']
                        targnode = self.nif.nodes[targname]
                    else:
                        #log.debug(f"Exporting collision {coll.name}, exported objects are {self.objs_written.keys()}")
                        if p.name not in self.objs_written:
                            targnode = self.export_shape_parents(coll)
                        else:
                            targnode = self.objs_written[p.name]

                    #log.debug(f"Writing collision object {coll.name} under {targnode}")
                    self.nif.add_collision(targnode, targnode, body, 
                            bhkCOFlags.parse(coll['pynCollisionFlags']).value)
                    self.objs_written[coll.name] = targnode


    def get_loop_partitions(self, face, loops, weights):
        vi1 = loops[face.loop_start].vertex_index
        p = set([k for k in weights[vi1].keys() if is_partition(k)])
        for i in range(face.loop_start+1, face.loop_start+face.loop_total):
            vi = loops[i].vertex_index
            p = p.intersection(set([k for k in weights[vi].keys() if is_partition(k)]))
    
        if len(p) != 1:
            face_verts = [lp.vertex_index for lp in loops[face.loop_start:face.loop_start+face.loop_total]]
            if len(p) == 0:
                log.warning(f'Face {face.index} has no partitions')
                self.warnings.add('NO_PARTITION')
                self.objs_no_part.add(self.active_obj)
                create_group_from_verts(self.active_obj, NO_PARTITION_GROUP, face_verts)
                return 0
            elif len(p) > 1:
                log.warning(f'Face {face.index} has too many partitions: {p}')
                self.warnings.add('MANY_PARITITON')
                self.objs_mult_part.add(self.active_obj)
                create_group_from_verts(self.active_obj, MULTIPLE_PARTITION_GROUP, face_verts)

        return p.pop()


    def extract_face_info(self, mesh, uvlayer, loopcolors, weights, obj_partitions, use_loop_normals=False):
        """ Extract triangularized face info from the mesh. 
            Return 
            loops = [vert-index, ...] list of vert indices in loops. Triangularized, 
                so these are to be read in triples.
            uvs = [(u,v), ...] list of uv coordinates 1:1 with loops
            norms = [(x,y,z), ...] list of normal vectors 1:1 with loops
                --Normal vectors come from the loops, because they reflect whether the edges
                are sharp or the object has flat shading
            colors = [(r,g,b,a), ...] 1:1 with loops
            partition_map = [n, ...] list of partition IDs, 1:1 with tris 

        """
        loops = []
        uvs = []
        orig_uvs = []
        norms = []
        colors = []
        partition_map = []

        # Calculating normals messes up the passed-in UV, so get the data out of it first
        for f in mesh.polygons:
            for i in f.loop_indices:
                orig_uvs.append(uvlayer[i].uv[:])
                ##log.debug(f"....Adding uv index {uvlayer[i].uv[:]}")

        # CANNOT figure out how to get the loop normals correctly.  They seem to follow the
        # face normals even on smooth shading.  (TEST_NORMAL_SEAM tests for this.) So use the
        # vertex normal except when there are custom split normals.
        bpy.ops.object.mode_set(mode='OBJECT') #required to get accurate normals
        mesh.calc_normals()
        mesh.calc_normals_split()

        def write_loop_vert(loopseg):
            """ Write one vert, given as a MeshLoop 
            """
            loops.append(loopseg.vertex_index)
            uvs.append(orig_uvs[loopseg.index])
            #if colormap or alphamap:
            #    colors.append(get_loop_color(mesh, loopseg.index, colormap, alphamap))
            if loopcolors:
                colors.append(loopcolors[loopseg.index])
            if use_loop_normals:
                norms.append(loopseg.normal[:])
            else:
                norms.append(mesh.vertices[loopseg.vertex_index].normal[:])

        # Write out the loops as triangles, and partitions to match
        for f in mesh.polygons:
            if f.loop_total < 3:
                log.warning(f"Degenerate polygons on {mesh.name}: 0={l0}, 1={l1}")
            else:
                if obj_partitions and len(obj_partitions) > 0:
                    loop_partition = self.get_loop_partitions(f, mesh.loops, weights)
                ##log.debug(f"Writing verts for polygon start={f.loop_start}, total={f.loop_total}, partition={loop_partition}")
                l0 = mesh.loops[f.loop_start]
                l1 = mesh.loops[f.loop_start+1]
                for i in range(f.loop_start+2, f.loop_start+f.loop_total):
                    loopseg = mesh.loops[i]

                    ##log.debug(f"Writing triangle: [{l0.vertex_index}, {l1.vertex_index}, {loopseg.vertex_index}]")
                    write_loop_vert(l0)
                    write_loop_vert(l1)
                    write_loop_vert(loopseg)
                    if obj_partitions and len(obj_partitions) > 0:
                        if loop_partition:
                            partition_map.append(obj_partitions[loop_partition].id)
                        else:
                            log.warning(f"Writing first partition for face without partitions {obj_partitions}")
                            partition_map.append(next(iter(obj_partitions.values())).id)
                    ##log.debug(f"Created tri with partition {loop_partition}")
                    l1 = loopseg

        ##log.debug(f"extract_face_info: loops = {loops[0:9]}")
        return loops, uvs, norms, colors, partition_map


    def export_partitions(self, obj, weights_by_vert, tris):
        """ Export partitions described by vertex groups
            weights = [dict[group-name: weight], ...] vertex weights, 1:1 with verts. For 
                partitions, can assume the weights are 1.0
            tris = [(v1, v2, v3)...] where v1-3 are indices into the vertex list
            returns (partitions, tri_indices)
                partitions = list of partition objects
                tri_indices = list of paritition indices, 1:1 with the shape's tri list
        """
        #log.debug(f"..Exporting partitions")
        partitions = partitions_from_vert_groups(obj)
        ##log.debug(f"....Found partitions {list(partitions.keys())}")

        if len(partitions) == 0:
            return [], []

        partition_set = set(list(partitions.keys()))

        tri_indices = [0] * len(tris)

        for i, t in enumerate(tris):
            # All 3 have to be in the same vertex group to count
            vg0 = all_vertex_groups(weights_by_vert[t[0]])
            vg1 = all_vertex_groups(weights_by_vert[t[1]])
            vg2 = all_vertex_groups(weights_by_vert[t[2]])
            tri_partitions = vg0.intersection(vg1).intersection(vg2).intersection(partition_set)
            if len(tri_partitions) > 0:
                if len(tri_partitions) > 1:
                    log.warning(f"Found multiple partitions for tri {t} in object {obj.name}: {tri_partitions}")
                    self.warnings.add('MANY_PARITITON')
                    self.objs_mult_part.add(obj)
                    create_group_from_verts(obj, MULTIPLE_PARTITION_GROUP, t)
                    # #log.debug(f"Number of verts in multiple partitions: {len(obj.vertex_groups[MULTIPLE_PARTITION_GROUP])}")

                # Triangulation may put some tris in two partitions. Just choose one--
                # exact division doesn't matter (if it did user should have put in an edge)
                tri_indices[i] = partitions[next(iter(tri_partitions))].id
            else:
                log.warning(f"Tri {t} is not assigned any partition")
                self.warnings.add('NO_PARTITION')
                self.objs_no_part.add(obj)
                create_group_from_verts(obj, NO_PARTITION_GROUP, t)

        ##log.debug(f"Partitions for export: {partitions.keys()}, {tri_indices[0:20]}")
        return list(partitions.values()), tri_indices


    def extract_colors(self, mesh):
        """Extract vertex color data from the given mesh. Use the VERTEX_ALPHA color map
            for alpha values if it exists.
            Returns [c.color[:] for c in editmesh.vertex_colors.active.data]
                This is 1:1 with loops
            """
        vc = mesh.vertex_colors
        alphamap = None
        alphamapname = ''
        colormap = None
        colormapname = ''
        colorlen = 0
        if ALPHA_MAP_NAME in vc.keys():
            alphamap = vc[ALPHA_MAP_NAME].data
            alphamapname = ALPHA_MAP_NAME
            colorlen = len(alphamap)
        if vc.active.data == alphamap:
            # Alpha map is active--see if theres another map to use for colors. If not, 
            # colors will be set to white
            for c in vc:
                if c.data != alphamap:
                    colormap = c.data
                    colormapname = c.name
                    break
        else:
            colormap = vc.active.data
            colormapname = vc.active.name
            colorlen = len(colormap)

        #log.debug(f"...Writing vertex colors from map {colormapname}, vertex alpha from {alphamapname}")
        loopcolors = [(0.0, 0.0, 0.0, 0.0)] * colorlen
        for i in range(0, colorlen):
            if colormap:
                c = colormap[i].color[:]
            else:
                c = (1.0, 1.0, 1.0, 1.0)
            if alphamap:
                a = alphamap[i].color
                c = (c[0], c[1], c[2], (a[0] + a[1] + a[2])/3)
            loopcolors[i] = c

        return loopcolors


    def extract_mesh_data(self, obj, arma, target_key):
        """ 
        Extract the triangularized mesh data from the given object
            obj = object being exported
            arma = controlling armature, if any. Needed so we can limit bone weights.
            target_key = shape key to export
        returns
            verts = list of XYZ vertex locations
            norms_new = list of XYZ normal values, 1:1 with verts
            uvmap_new = list of (u, v) values, 1:1 with verts
            colors_new = list of RGBA color values 1:1 with verts. May be None.
            tris = list of (t1, t2, t3) vert indices to define triangles
            weights_by_vert = [dict[group-name: weight], ...] 1:1 with verts
            morphdict = {shape-key: [verts...], ...} XXX>only if "target_key" is NOT specified
        NOTE this routine changes selection and switches to edit mode and back
        """
        loopcolors = None
        saved_sk = obj.active_shape_key_index
        
        try:
            ObjectSelect([obj])
            ObjectActive(obj)
                
            # This next little dance ensures the mesh.vertices locations are correct
            if self.export_modifiers:
                depsgraph = bpy.context.evaluated_depsgraph_get()
                obj1 = obj.evaluated_get(depsgraph) 
            else:
                obj1 = obj           
            obj1.active_shape_key_index = 0
            bpy.ops.object.mode_set(mode = 'EDIT')
            bpy.ops.object.mode_set(mode = 'OBJECT')
            editmesh = obj1.data
            editmesh.update()
         
            verts, weights_by_vert, morphdict \
                = extract_vert_info(obj1, editmesh, arma, target_key, self.scale)
        
            # Pull out vertex colors first because trying to access them later crashes
            bpy.ops.object.mode_set(mode = 'OBJECT') # Required to get vertex colors
            if len(editmesh.vertex_colors) > 0:
                loopcolors = self.extract_colors(editmesh)
        
            # Apply shape key verts to the mesh so normals will be correct.  If the mesh has
            # custom normals, fukkit -- use the custom normals and assume the deformation
            # won't be so great that it looks bad.
            bpy.ops.object.mode_set(mode = 'OBJECT') 
            uvlayer = editmesh.uv_layers.active.data
            if target_key != '' and \
                editmesh.shape_keys and \
                target_key in editmesh.shape_keys.key_blocks.keys() and \
                not editmesh.has_custom_normals:
                editmesh = mesh_from_key(editmesh, verts, target_key)
                    
            # Extracting and triangularizing
            partitions = partitions_from_vert_groups(obj1)
            loops, uvs, norms, loopcolors, partition_map = \
                self.extract_face_info(
                    editmesh, uvlayer, loopcolors, weights_by_vert, partitions,
                    use_loop_normals=editmesh.has_custom_normals)
        
            mesh_split_by_uv(verts, loops, norms, uvs, weights_by_vert, morphdict)

            # Make uv and norm lists 1:1 with verts (rather than with loops)
            uvmap_new = [(0.0, 0.0)] * len(verts)
            norms_new = [(0.0, 0.0, 0.0)] * len(verts)
            for i, vi in enumerate(loops):
                assert vi < len(verts), f"Error: Invalid vert index in loops: {vi} >= {len(verts)}"
                uvmap_new[vi] = uvs[i]
                norms_new[vi] = norms[i]
        
            ## Our "loops" list matches 1:1 with the mesh's loops. So we can use the polygons
            ## to pull the loops
            tris = []
            for i in range(0, len(loops), 3):
                tris.append((loops[i], loops[i+1], loops[i+2]))
        
            colors_new = None
            if len(loopcolors) > 0:
                #log.debug(f"Exporting vertex colors for shape {obj.name}")
                colors_new = [(0.0, 0.0, 0.0, 0.0)] * len(verts)
                for i, lp in enumerate(loops):
                    colors_new[lp] = loopcolors[i]
        
        finally:
            #obj.rotation_euler = original_rot
            #obj.data = originalmesh
            obj.active_shape_key_index = saved_sk
            pass

        return verts, norms_new, uvmap_new, colors_new, tris, weights_by_vert, \
            morphdict, partitions, partition_map


    def export_shape_parents(self, obj) -> NiNode:
        """Export any parent NiNodes the shape might need 
        Returns the nif node that should be the parent of the shape (may be None)
        """
        # ancestors list contains all parents from root to obj's immediate parent
        ancestors = []
        p = obj.parent
        while p:
            ancestors.insert(0, p)
            p = p.parent
        #log.debug(f"Shape {obj.name} has parents {ancestors}")

        last_parent = None
        ninode = None
        for p in ancestors:
            if p.type == 'EMPTY' and 'pynBlock_Name' in p:
                if p.name in self.objs_written:
                    ninode = self.objs_written[p.name]
                    last_parent = ninode
                else:
                    xf = TransformBuf.from_matrix(apply_scale_xf(p.matrix_local, 1/self.scale))
                    #LogIfBone(p.name, f"Writing transform for parent node {p.name}:\n{xf}")
                    ninode = self.nif.add_node(p.name, xf, last_parent)
                    #log.debug(f"Writing shape parent {p.name} as {ninode.name} with parent {last_parent.name if last_parent else '<none>'}")
                    last_parent = ninode
                    self.objs_written[p.name] = ninode
                    collisions = [x for x in p.children if x.name.startswith("bhkCollisionObject")]
                    if len(collisions) > 0:
                        self.export_collisions(collisions)
        
        return ninode


    def get_bone_xforms(self, arma, bone_names, shape):
        """Return transforms for the bones in list. Checks the "preserve_hierarchy" flag to 
        determine whether to return global or local transforms.
            arma = armature
            bone_names = list of names
            shape = shape being exported
            result = dict{bone-name: MatTransform, ...}
        """
        result = {}
        for b in arma.data.bones:
            result[b.name] = get_bone_xform(arma, b.name, self.game, 
                                            self.preserve_hierarchy,
                                            self.export_pose)
    
        return result

    def write_bone(self, shape:NiShape, arma, bone_name, bones_to_write):
        """ 
        Write a shape's bone, writing all parent bones first if necessary Returns the name
        of the node in the target nif for the new bone. 
        
        * shape - bone is added to shape's skin. May be None, if only writing a skeleton
        * arma - parent armature
        * bone_name - bone to write (blender name)
        * bones_to_write - list of bones that the shape needs. If the bone isn't in this
          list, only write it if it's needed for the hierarchy.
        """
        if bone_name in self.writtenbones:
            return self.writtenbones[bone_name]

        if not bone_name in bones_to_write and not self.preserve_hierarchy:
            return None

        bone_parent = arma.data.bones[bone_name].parent
        parname = None
        if bone_parent:
            parname = self.write_bone(shape, arma, bone_parent.name, bones_to_write)
        
        nifname = self.nif_name(bone_name)

        xf = get_bone_xform(arma, bone_name, self.game, 
                            self.preserve_hierarchy,
                            self.export_pose)
        tb = pack_xf_to_buf(xf, self.scale)

        LogIfBone(nifname, f"<write_bone> writing bone {bone_name} with transform\n{tb}")
        
        if bone_name in bones_to_write and shape:
            shape.add_bone(nifname, tb, 
                           (parname if self.preserve_hierarchy else None))
        elif self.preserve_hierarchy:
            # Not a shape bone but needed for the hierarchy
            self.nif.add_node(nifname, tb, parname)
        
        self.writtenbones[bone_name] = nifname
        return nifname


    def write_bone_hierarchy(self, shape:NiShape, arma, used_bones:list):
        """Write the bone hierarchy to the nif. Do this first so that transforms 
        and parent/child relationships are correct. Do not assume that the skeleton is fully
        connected (do Blender armatures have to be fully connected?). 
        used_bones - list of bone names to write. 
        """
        self.writtenbones = {}
        for bone_name in used_bones:
            if bone_name in arma.data.bones:
                self.write_bone(shape, arma, bone_name)


    def export_skin(self, obj, arma, new_shape, new_xform, weights_by_vert):
        """
        Export the skin for a shape, including bones used by the skin.
        """
        log.info(f"Skinning {obj.name}")
        new_shape.skin()
        new_shape.transform = TransformBuf.from_matrix(new_xform)
        newxfi = new_xform.copy()
        newxfi.invert()
        new_shape.set_global_to_skin(TransformBuf.from_matrix(newxfi))
    
        weights_by_bone = get_weights_by_bone(weights_by_vert, arma.data.bones.keys())

        self.writtenbones = {}
        for bone_name in  weights_by_bone.keys():
            self.write_bone(new_shape, arma, bone_name, weights_by_bone.keys())

        for bone_name, bone_weights in weights_by_bone.items():
            nifname = self.nif_name(bone_name)
            LogIfBone(bone_name, f"<export_skin> writing {bone_name}")
            if self.export_pose:
                # Bind location is different from pose location
                xf = get_bone_xform(arma, bone_name, self.game, False, False)
                xfoffs = obj.matrix_world.inverted() @ xf
                xfinv = xfoffs.inverted()
                tb_bind = pack_xf_to_buf(xfinv, self.scale)
                new_shape.set_skin_to_bone_xform(nifname, tb_bind)
                LogIfBone(nifname, f"<export_skin>({obj.name}) writing bone {bone_name} with sk2b transform\n{xfinv}\n->nif\n{tb_bind}")
            else:
                # Have to set skin-to-bone again because adding the bones nuked it
                xf = get_bone_xform(arma, bone_name, self.game, False, self.export_pose)
                xfoffs = obj.matrix_world.inverted() @ xf
                xfinv = xfoffs.inverted()
                LogIfBone(bone_name, f"Have bone transform\n{xf}")
                LogIfBone(bone_name, f"Have offset transform\n{xfoffs}")
                LogIfBone(bone_name, f"Bone transform inverted\n{xfinv}")
                tb = pack_xf_to_buf(xfinv, self.scale)
                new_shape.set_skin_to_bone_xform(nifname, tb)

            self.writtenbones[bone_name] = nifname
            new_shape.setShapeWeights(nifname, bone_weights)


    def apply_shape_key(self, key_name):
        pass


    def export_shape(self, obj, target_key='', arma=None):
        """ Export given blender object to the given NIF file; also writes any associated
            tri file. Checks to make sure the object wasn't already written.
            obj = blender object
            target_key = shape key to export
            arma = armature to skin to
            """
        if obj.name in self.objs_written or nonunique_name(obj) in collision_names:
            return
        log.info(f"Exporting {obj.name}")

        self.active_obj = obj

        # If there's a hierarchy, export parents (recursively) first
        my_parent = self.export_shape_parents(obj)

        retval = set()

        # Prepare for reporting any bone weight errors
        is_skinned = (arma is not None)
        unweighted = []
        if UNWEIGHTED_VERTEX_GROUP in obj.vertex_groups:
            obj.vertex_groups.remove(obj.vertex_groups[UNWEIGHTED_VERTEX_GROUP])
        if MULTIPLE_PARTITION_GROUP in obj.vertex_groups:
            obj.vertex_groups.remove(obj.vertex_groups[MULTIPLE_PARTITION_GROUP])
        if NO_PARTITION_GROUP in obj.vertex_groups:
            obj.vertex_groups.remove(obj.vertex_groups[NO_PARTITION_GROUP])
        
        if is_skinned:
            # Get unweighted bones before we muck up the list by splitting edges
            unweighted = tag_unweighted(obj, arma.data.bones.keys())
            if not expected_game(self.nif, arma.data.bones):
                log.warning(f"Exporting to game that doesn't match armature: game={self.nif.game}, armature={arma.name}")
                retval.add('GAME')

        # Collect key info about the mesh 
        verts, norms_new, uvmap_new, colors_new, tris, weights_by_vert, morphdict, partitions, partition_map = \
           self.extract_mesh_data(self.active_obj, arma, target_key)

        is_headpart = obj.data.shape_keys \
                and len(self.nif.dict.expression_filter(set(obj.data.shape_keys.key_blocks.keys()))) > 0

        obj.data.update()
        shaderexp = shader_io.ShaderExporter(obj)

        if shaderexp.is_obj_space:
            norms_exp = None
        else:
            norms_exp = norms_new

        # Make the shape in the nif file
        #log.debug(f"..Exporting '{obj.name}' to nif: {len(verts)} vertices, {len(tris)} tris, parent {my_parent}")
        new_shape = self.nif.createShapeFromData(nonunique_name(obj), 
                                                 verts, tris, uvmap_new, norms_exp,
                                                 is_headpart, is_skinned, 
                                                 shaderexp.is_effectshader,
                                                 parent=my_parent)
        if colors_new:
            new_shape.set_colors(colors_new)

        self.export_shape_data(obj, new_shape)
        
        # try:
        #     # Write the shader
        shaderexp.export(new_shape)
        # except:
        #     log.warning(f"Couldn't parse the shader nodes on {obj.name}")
        #     self.warnings.add('WARNING')

        # Using local transform because the shapes will be parented in the nif
        new_xform = obj.matrix_local * (1/self.scale) 
        if not has_uniform_scale(obj):
            # Non-uniform scales applied to verts, so just use 1.0 for the scale on the object
            l, r, s = new_xform.decompose()
            new_xform = MatrixLocRotScale(l, r, Vector((1,1,1))) 
        elif  not NearEqual(self.scale, 1.0):
            # Export scale factor applied to verts, so scale obj translation but not obj scale 
            l, r, s = new_xform.decompose()
            new_xform = MatrixLocRotScale(l, r, obj.matrix_local.to_scale()) 
        
        if is_skinned:
            self.export_skin(self.active_obj, arma, new_shape, new_xform, weights_by_vert)
            if len(unweighted) > 0:
                create_group_from_verts(obj, UNWEIGHTED_VERTEX_GROUP, unweighted)
                log.warning(f"Some vertices are not weighted to the armature in object {obj.name}")
                self.objs_unweighted.add(obj)

            if len(partitions) > 0:
                if 'FO4_SEGMENT_FILE' in obj.keys():
                    #log.debug(f"....Writing segment file {obj['FO4_SEGMENT_FILE']}")
                    new_shape.segment_file = obj['FO4_SEGMENT_FILE']

                # #log.debug(f"Partitions for export: {partitions.keys()}, {partition_map[0:20]}")
                new_shape.set_partitions(partitions.values(), partition_map)

            self.export_collisions([c for c in arma.children if c.name.startswith("bhkCollisionObject")])
        else:
            new_shape.transform = TransformBuf.from_matrix(new_xform)

        # Write collisions
        self.export_collisions([c for c in obj.children if c.name.startswith("bhkCollisionObject")])

        # Write tri file
        retval |= self.export_tris(obj, verts, tris, uvmap_new, morphdict)

        # Write TRIP extra data if this is Skyrim
        if self.write_bodytri \
            and self.game in ['SKYRIM', 'SKYRIMSE'] \
            and len(self.trip.shapes) > 0:
            new_shape.string_data = [('BODYTRI', truncate_filename(self.trippath, "meshes"))]

        # Remember what we did as defaults for next time
        self.objs_written[obj.name] = new_shape

        obj['PYN_GAME'] = self.game
        if self.scale != SCALE_DEF: obj['PYN_SCALE_FACTOR'] = self.scale 
        if self.preserve_hierarchy != PRESERVE_HIERARCHY_DEF:
            obj['PYN_PRESERVE_HIERARCHY'] = self.preserve_hierarchy 
        if arma:
            arma['PYN_RENAME_BONES'] = self.rename_bones
            if self.rename_bones_nift != RENAME_BONES_NIFT_DEF:
                arma['PYN_RENAME_BONES_NIFTOOLS'] = self.rename_bones_nift 
        if self.write_bodytri != WRITE_BODYTRI_DEF:
            obj['PYN_WRITE_BODYTRI_ED'] = self.write_bodytri 
        if self.export_pose != EXPORT_POSE_DEF: obj['PYN_EXPORT_POSE'] = self.export_pose 

        if self.active_obj != obj:
            bpy.data.meshes.remove(self.active_obj.data)
            self.active_obj = None

        log.info(f"{obj.name} successfully exported to {self.nif.filepath}\n")
        return retval
    

    def export_armature(self, arma):
        """Export an armature with no shapes"""
        for b in arma.data.bones:
            self.write_bone(None, arma, b.name, arma.data.bones.keys())


    def export_file_set(self, suffix=''):
        """ Create a set of nif files from the given object, using the given armature and appending
            the suffix. One file is created per shape key with the shape key used as suffix. Associated
            TRIP files are exported if there is TRIP info.
                suffix = suffix to append to the filenames, after the shape key suffix. 
                    Empty string for regular nifs, non-empty for facebones nifs
            """
        if self.file_keys is None or len(self.file_keys) == 0:
            shape_keys = ['']
        else:
            shape_keys = self.file_keys

        # One TRIP file is written even if we have variants of the mesh ("_" prefix)
        fname_ext = os.path.splitext(os.path.basename(self.filepath))
        self.trip = TripFile()
        self.trippath = os.path.join(os.path.dirname(self.filepath), fname_ext[0]) + ".tri"

        for sk in shape_keys:
            fbasename = fname_ext[0] + sk + suffix
            fnamefull = fbasename + fname_ext[1]
            fpath = os.path.join(os.path.dirname(self.filepath), fnamefull)

            self.objs_written.clear()
            self.nif = NifFile()

            rt = "NiNode"
            rn = "Scene Root"

            if self.objects:
                shape = next(iter(self.objects))
            else:
                shape = self.armature

            if "pynRootNode_BlockType" in shape:
                rt = shape["pynRootNode_BlockType"]
            if "pynRootNode_Name" in shape:
                rn = shape["pynRootNode_Name"]
            
            self.nif.initialize(self.game, fpath, rt, rn)
            if "pynRootNode_Flags" in shape:
                #log.debug(f"Root node flags are '{shape['pynRootNode_Flags']}' = '{RootFlags.parse(shape['pynRootNode_Flags']).value}'")
                self.nif.rootNode.flags = RootFlags.parse(shape["pynRootNode_Flags"]).value

            if suffix == '_faceBones':
                self.nif.dict = fo4FaceDict

            self.nif.dict.use_niftools = self.rename_bones_nift
            self.writtenbones = {}

            if self.objects:
                for obj in self.objects:
                    #arma, fb_arma = find_armatures(obj)
                    if suffix == "_faceBones" and self.facebones:
                        # Have exporting the facebones variant and have a facebones armature
                        self.export_shape(obj, sk, self.facebones)
                    elif (not suffix) and self.armature:
                        # Exporting the main file and have an armature to do it with. 
                        self.export_shape(obj, sk, self.armature)
                    elif (not suffix) and self.facebones:
                        # Exporting the main file and have a facebones armature to do it
                        # with. Facebones armatures generally have all the necessary bones
                        # for export, so it's fine to use them.
                        self.export_shape(obj, sk, self.facebones)
                    elif (not self.facebones) and (not self.armature):
                        # No armatures, just export the shape.
                        self.export_shape(obj, sk)
            elif self.armature:
                # Just export the skeleton
                self.export_armature(self.armature)

            # Check for bodytri morphs--write the extra data node if needed
            ##log.debug(f"TRIP data: shapes={len(self.trip.shapes)}, bodytri written: {self.bodytri_written}, filepath: {truncate_filename(self.trippath, 'meshes')}")
            if self.write_bodytri \
                and self.game in ['FO4', 'FO76'] \
                and len(self.trip.shapes) > 0 \
                and  not self.bodytri_written:
                self.nif.string_data = [('BODYTRI', truncate_filename(self.trippath, "meshes"))]

            self.export_collisions([c for c in self.collisions if c.parent == None])
            self.export_extra_data()

            self.nif.save()
            log.info(f"..Wrote {fpath}")
            msgs = list(filter(lambda x: not x.startswith('Info: Loaded skeleton') and len(x)>0, 
                               self.nif.message_log().split('\n')))
            if msgs:
                self.message_log.append(self.nif.message_log())

        if len(self.trip.shapes) > 0:
            #log.debug(f"First shape in trip file has shapes: {self.trip.shapes[next(iter(self.trip.shapes))].keys()}")
            self.trip.write(self.trippath)
            log.info(f"..Wrote {self.trippath}")


    def execute(self):
        if not self.objects and not self.armature:
            log.warning(f"No objects selected for export")
            self.warnings.add('NOTHING')
            return

        log.info(str(self))
        NifFile.clear_log()
        self.export_file_set('')
        if self.facebones:
            self.export_file_set('_faceBones')
        #if self.armature:
        #    self.export_file_set('')
        #if self.facebones is None and self.armature is None:
        msgs = list(filter(lambda x: not x.startswith('Info: Loaded skeleton') and len(x)>0, 
                           NifFile.message_log().split('\n')))
        if msgs:
            log.debug("Nifly Message Log:\n" + NifFile.message_log())
    
    def export(self, objects):
        self.set_objects(objects)
        self.execute()

    @classmethod
    def do_export(cls, filepath, game, objects, scale=1.0):
        return NifExporter(filepath, game, scale=scale).export(objects)
        
def get_default_scale():
    # #log.debug(f"<get_default_scale {bpy.context.selected_objects}")
    # if bpy.context.active_object:
        # if 'PYN_SCALE_FACTOR' in bpy.context.active_object:
        #     return bpy.context.active_object['PYN_SCALE_FACTOR']
    # try:
    #     for obj in bpy.context.selected_objects:
    #         if 'PYN_SCALE_FACTOR' in obj:
    #             return obj['PYN_SCALE_FACTOR']
    # except Exception as err:
    #     #log.debug(f"error: {err}")
    #     return 1.0
    return 1.0
    
class ExportNIF(bpy.types.Operator, ExportHelper):
    """Export Blender object(s) to a NIF File"""

    bl_idname = "export_scene.pynifly"
    bl_label = 'Export NIF (Nifly)'
    bl_options = {'PRESET'}

    filename_ext = ".nif"

    target_game: EnumProperty(
            name="Target Game",
            items=(('SKYRIM', "Skyrim", ""),
                   ('SKYRIMSE', "Skyrim SE", ""),
                   ('FO4', "Fallout 4", ""),
                   ('FO76', "Fallout 76", ""),
                   ('FO3', "Fallout New Vegas", ""),
                   ('FO3', "Fallout 3", ""),
                   )
            )

    scale_factor: bpy.props.FloatProperty(
        name="Scale correction",
        description="Change scale for export - set to 0.1 to match NifTools default.",
        default=1.0
        )
    
    rename_bones: bpy.props.BoolProperty(
        name="Rename Bones",
        description="Rename bones from Blender conventions back to nif.",
        default=True)

    rename_bones_niftools: bpy.props.BoolProperty(
        name="Rename Bones as per NifTools",
        description="Rename bones from NifTools' Blender conventions back to nif.",
        default=False)

    preserve_hierarchy: bpy.props.BoolProperty(
        name="Preserve Bone Hierarchy",
        description="Preserve bone hierarchy in exported nif.",
        default=False)

    write_bodytri: bpy.props.BoolProperty(
        name="Export BODYTRI Extra Data",
        description="Write an extra data node pointing to the BODYTRI file, if there are any bodytri shape keys. Not needed if exporting for Bodyslide, because they write their own.",
        default=False)

    export_pose: bpy.props.BoolProperty(
        name="Export pose position",
        description="Export bones in pose position.",
        default=False)
    
    export_modifiers: bpy.props.BoolProperty(
        name="Export modifiers",
        description="Export all active modifiers (including shape keys)",
        default=False)

    chargen_ext: bpy.props.StringProperty(
        name="Chargen extension",
        description="Extension to use for chargen files (not including file extension).",
        default="chargen")
    

    def __init__(self):
        self.objects_to_export = get_export_objects(bpy.context)

        if len(self.objects_to_export) == 0:
            self.report({"ERROR"}, "No objects selected for export")
            return

        obj = self.objects_to_export[0]
        if not self.filepath or self.filepath == '':
            self.filepath = clean_filename(obj.name)

        export_armature = None
        if obj.type == 'ARMATURE':
            export_armature = obj
        else:
            export_armature, fb_arma = find_armatures(obj)
            if not export_armature:
                export_armature = fb_arma

        g = ""
        if 'PYN_GAME' in obj:
            g = obj['PYN_GAME']
        else:
            if export_armature:
                g = best_game_fit(export_armature.data.bones)
        if g != "":
            self.target_game = g
        
        if obj and 'PYN_SCALE_FACTOR' in obj:
            self.scale_factor = obj['PYN_SCALE_FACTOR']
        elif export_armature and 'PYN_SCALE_FACTOR' in export_armature:
            self.scale_factor = export_armature['PYN_SCALE_FACTOR']

        if export_armature and 'PYN_RENAME_BONES' in export_armature:
            self.rename_bones = export_armature['PYN_RENAME_BONES']

        if export_armature and 'PYN_RENAME_BONES_NIFTOOLS' in export_armature:
            self.rename_bones_niftools = export_armature['PYN_RENAME_BONES_NIFTOOLS']

        if obj and 'PYN_PRESERVE_HIERARCHY' in obj:
            self.preserve_hierarchy = obj['PYN_PRESERVE_HIERARCHY']

        if obj and 'PYN_WRITE_BODYTRI_ED' in obj:
            self.write_bodytri = obj['PYN_WRITE_BODYTRI_ED']

        if obj and 'PYN_EXPORT_POSE' in obj:
            self.export_pose = obj['PYN_EXPORT_POSE']

        if obj and 'PYN_CHARGEN_EXT' in obj:
            self.chargen_ext = obj['PYN_CHARGEN_EXT']

        
    @classmethod
    def poll(cls, context):
        if len(context.selected_objects) == 0:
            log.error("Must select an object to export")
            return False

        if context.object.mode != 'OBJECT':
            log.error("Must be in Object Mode to export")
            return False

        return True

    def execute(self, context):
        res = set()

        if not self.poll(context):
            self.report({"ERROR"}, f"Cannot run exporter--see system console for details")
            return {'CANCELLED'} 

        if len(self.objects_to_export) == 0:
            self.report({"ERROR"}, "No objects selected for export")
            return {'CANCELLED'}

        LogStart(bl_info, "EXPORT", "NIF")
        NifFile.Load(nifly_path)

        try:
            exporter = NifExporter(self.filepath, 
                                   self.target_game, 
                                   chargen=self.chargen_ext, 
                                   scale=self.scale_factor)

            exporter.rename_bones = self.rename_bones
            exporter.rename_bones_nift = self.rename_bones_niftools
            exporter.preserve_hierarchy = self.preserve_hierarchy
            exporter.write_bodytri = self.write_bodytri
            exporter.export_pose = self.export_pose
            exporter.export_modifiers = self.export_modifiers
            exporter.export(self.objects_to_export)
            
            rep = False
            status = {"SUCCESS"}
            if len(exporter.objs_unweighted) > 0:
                status = {"ERROR"}
                self.report(status, f"The following objects have unweighted vertices.See the '*UNWEIGHTED*' vertex group to find them: \n{exporter.objs_unweighted}")
                rep = True
            if len(exporter.objs_scale) > 0:
                status = {"ERROR"}
                self.report(status, f"The following objects have non-uniform scale, which nifs do not support. Scale applied to verts before export.\n{exporter.objs_scale}")
                rep = True
            if len(exporter.objs_mult_part) > 0:
                status = {"WARNING"}
                self.report(status, f"Some faces have been assigned to more than one partition, which should never happen.\n{exporter.objs_mult_part}")
                rep = True
            if len(exporter.objs_no_part) > 0:
                status = {"WARNING"}
                self.report(status, f"Some faces have been assigned to no partition, which should not happen for skinned body parts.\n{exporter.objs_no_part}")
                rep = True
            if len(exporter.arma_game) > 0:
                status = {"WARNING"}
                self.report(status, f"The armature appears to be designed for a different game--check that it's correct\nArmature: {exporter.arma_game}, game: {exporter.game}")
                rep = True
            if 'NOTHING' in exporter.warnings:
                status = {"WARNING"}
                self.report(status, f"No mesh selected; nothing to export")
                rep = True
            if 'WARNING' in exporter.warnings:
                status = {"WARNING"}
                self.report(status, f"Export completed with warnings. Check the console window.")
                rep = True
            if not rep:
                self.report({'INFO'}, f"Export successful")
            LogFinish("EXPORT", self.objects_to_export, status, False)
            
        except:
            log.exception("Export of nif failed")
            self.report({"ERROR"}, "Export of nif failed, see console window for details")
            res.add("CANCELLED")
            LogFinish("EXPORT", self.objects_to_export, {"ERROR"}, False)

        return res.intersection({'CANCELLED'}, {'FINISHED'})

#----------
class PynRenamerNifTools(bpy.types.Operator):
    """Rename bones from PyNifly to NifTools"""
    bl_idname = "object.pynifly_rename_niftools"        # Unique identifier for buttons and menu items to reference.
    bl_label = "PyNifly: Rename bones to NifTools"         # Display name in the interface.
    bl_options = {'REGISTER', 'UNDO'}  # Enable undo for the operator.

    def execute(self, context):        # execute() is called when running the operator.
        found_work = False

        scene = context.scene
        for obj in scene.objects:
            if obj.type == "ARMATURE" and obj.select_get():
                log.info(f"Renaming bones on {obj}")
                found_work = True
                for b in obj.data.bones:
                    try:
                        #log.debug(f"Attempting rename of {b.name}")
                        b.name = skyrimDict.byPynifly[b.name].niftools
                        #log.debug(f"Renamed {b.name}")
                    except:
                        pass
                try:
                    obj.data.niftools.axis_forward = "Z"
                    obj.data.niftools.axis_up = "-X"
                except:
                    pass

        if not found_work:
            log.warning(f"No valid objects found to do renaming on")
            return {'WARNING'}
        else:
            return {'FINISHED'}            # Lets Blender know the operator finished successfully.

def nifly_menu_rename_niftools(self, context):
    self.layout.operator(PynRenamerNifTools.bl_idname)

#------------

def nifly_menu_import_nif(self, context):
    self.layout.operator(ImportNIF.bl_idname, text="Nif file with Nifly (.nif)")
def nifly_menu_import_tri(self, context):
    self.layout.operator(ImportTRI.bl_idname, text="Tri file with Nifly (.tri)")
def nifly_menu_export(self, context):
    self.layout.operator(ExportNIF.bl_idname, text="Nif file with Nifly (.nif)")

def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(nifly_menu_import_nif)
    bpy.types.TOPBAR_MT_file_import.remove(nifly_menu_import_tri)
    bpy.types.TOPBAR_MT_file_export.remove(nifly_menu_export)
    bpy.types.VIEW3D_MT_object.remove(nifly_menu_rename_niftools)
    try:
        bpy.utils.unregister_class(bpy.types.IMPORT_SCENE_OT_pynifly)
        bpy.utils.unregister_class(bpy.types.IMPORT_SCENE_OT_pyniflytri)
        bpy.utils.unregister_class(bpy.types.EXPORT_SCENE_OT_pynifly)
        bpy.utils.unregister_class(bpy.types.OBJECT_OT_pynifly_rename_niftools)
    except:
        pass
    skeleton_hkx.unregister()

def register():
    unregister()
    bpy.utils.register_class(ImportNIF)
    bpy.utils.register_class(ImportTRI)
    bpy.utils.register_class(ExportNIF)
    bpy.utils.register_class(PynRenamerNifTools)
    bpy.types.TOPBAR_MT_file_import.append(nifly_menu_import_nif)
    bpy.types.TOPBAR_MT_file_import.append(nifly_menu_import_tri)
    bpy.types.TOPBAR_MT_file_export.append(nifly_menu_export)
    bpy.types.VIEW3D_MT_object.append(nifly_menu_rename_niftools)
    skeleton_hkx.register()


if __name__ == "__main__":
    try:
        unregister()
    except:
        pass
    register()
