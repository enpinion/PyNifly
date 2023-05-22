"""Skeleton XML export/import for Blender"""

# Copyright © 2023, Bad Dog.

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
from blender_defs import *
import xml.etree.ElementTree as xml
import hashlib


bl_info = {
    "name": "NIF format",
    "description": "Nifly Import/Export for Skyrim, Skyrim SE, and Fallout 4 NIF files (*.nif)",
    "author": "Bad Dog",
    "blender": (3, 0, 0),
    "version": (9, 6, 2),  
    "location": "File > Import-Export",
    "support": "COMMUNITY",
    "category": "Import-Export"
}

numseqpat = re.compile("[\d\.\s-]+")
numberpat = re.compile("[\d\.-]+")

class SkeletonArmature():
    def __init__(self, name):
        """Make an armature to import a skeleton XML into. 
        Returns the armature, selected and active."""
        armdata = bpy.data.armatures.new(name)
        self.arma = bpy.data.objects.new(name, armdata)
        bpy.context.view_layer.active_layer_collection.collection.objects.link(self.arma)
        ObjectActive(self.arma)
        ObjectSelect([self.arma])


    def addbone(self, bonename, xform):
        """Create the bone in the armature. Assume armature is in edit mode."""
        log.debug(f"Adding bone {bonename} at \n{xform}")
        bone = self.arma.data.edit_bones.new(bonename)
        bone.matrix = xform

    
    def bones_from_xml(self, root):
        skel = root.find(".//*[@class='hkaSkeleton']")
        skelname = skel.find("./*[@name='name']").text
        skelindices = [int(x) for x in skel.find("./*[@name='parentIndices']").text.split()]
        log.debug(f"Skeleton name = {skelname}")
        log.debug(f"Skeleton indices = {skelindices}")

        bonelist = []
        skelbones = skel.find("./*[@name='bones']")
        for b in skelbones.iter('hkobject'):
            bonelist.append(b.find("./*[@name='name']").text)
        log.debug(f"Found bones {bonelist}")

        pose = skel.find("./*[@name='referencePose']")
        numseq = numseqpat.findall(pose.text)
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.mode_set(mode='EDIT')
        for i in range(0, len(bonelist)):
            log.debug(f"[{i}]")
            loc = Vector([float(x) for x in numseq[i*3].split()])
            rotlist = [float(x) for x in numseq[i*3+1].split()]
            rot = Quaternion(rotlist[1:4], rotlist[0])
            scale = Vector([float(x) for x in numseq[i*3+2].split()])
            log.debug(f"Location: {loc}")
            log.debug(f"Rotation: {rot}")
            log.debug(f"Scale: {scale}")
            create_bone(self.arma.data, bonelist[i], Matrix.LocRotScale(loc, rot, scale), 
                        "SKYRIM", 1.0, 0)
        bpy.ops.object.mode_set(mode='OBJECT')
        self.arma.update_from_editmode()


class ImportSkel(bpy.types.Operator, ImportHelper):
    """Import a skeleton XML file (unpacked from HXK)"""
    bl_idname = "import_scene.skeleton_hkx"
    bl_label = "Import Skeleton HKX (XML)"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".xml"
    filter_glob: StringProperty(
        default="*.xml",
        options={'HIDDEN'},
    )

    files: CollectionProperty(
        type=bpy.types.OperatorFileListElement,
        options={'HIDDEN', 'SKIP_SAVE'},)
    

    def execute(self, context):
        LogStart(bl_info, "IMPORT SKELETON", "XML")
        log.info(f"Importing {self.filepath}")
        infile = xml.parse(self.filepath)
        inroot = infile.getroot()
        log.debug(f"Root tag: {inroot.tag}")
        log.debug(f"Root attributes: {inroot.attrib}")
        log.debug(f"Children: {[x.attrib for x in inroot]}")
        sec1 = inroot[0]
        log.debug(f"First section: {sec1.attrib}")
        if inroot:
            arma = SkeletonArmature(Path(self.filepath).stem)
            arma.bones_from_xml(inroot)

        status = {'FINISHED'}

        return status


# ----------------------- EXPORT -------------------------------------

def set_param(elem, attribs, txt):
    p = xml.SubElement(elem, "hkparam")
    for n, v in attribs.items():
        p.set(n, v)
    p.text = txt
    return p


class ExportSkel(bpy.types.Operator, ExportHelper):
    """Export Blender armature to a skeleton HKX file"""

    bl_idname = "export_scene.skeleton_hkx"
    bl_label = 'Export skeleton HKX (XML)'
    bl_options = {'PRESET'}

    filename_ext = ".xml"

    xmltree = None
    root = None
    section = None
    rootlevelcontainer = None
    animationcontainer = None
    memoryresourcecontainer = None
    skeleton = None
    block_index = 50
    context = None

    def use_index(self) -> int:
        v = self.block_index
        self.block_index += 1
        return v
    

    def find_root(self, bones):
        for b in bones:
            if b.parent not in bones:
                return b
        return bones[0]


    def set_incr_name(self, elem):
        elem.set('name', "#{:04}".format(self.use_index()))


    def set_signature(self, elem, seed, strlist):
        h = hashlib.blake2b(seed.encode('utf-8'), digest_size=4)
        for s in strlist:
            h.update(s.encode('utf-8'))
        elem.set('signature', "0x" + h.hexdigest())


    def write_header(self) -> None:
        self.root = xml.Element('hkpackfile')
        self.root.set("classversion", "8")
        self.root.set("contentsversion", "hk_2010.2.0-r1")
        self.section = xml.SubElement(self.root, 'hksection')
        self.section.set("name", "__data__")
        self.xmltree = xml.ElementTree(self.root)

    def write_header_refs(self):
        self.root.set("toplevelobject", self.rootlevelcontainer.attrib["name"])


    def write_rootlevelcontainer(self):
        rlc = xml.SubElement(self.section, "hkobject")
        self.set_incr_name(rlc)
        rlc.set("class", "hkRootLevelContainer")
        self.set_signature(rlc, "hkRootLevelContainer", [])
        self.rootlevelcontainer = rlc

    def write_rootlevel_refs(self):
        v = set_param(self.rootlevelcontainer, {"name":"namedVariants", "numelements":"2"}, "")
        o1 = xml.SubElement(v, "hkobject")
        set_param(o1, {'name':"name"}, "Merged Animation Container")
        set_param(o1, {'name':"className"}, "hkaAnimationContainer")
        set_param(o1, {'name':"variant"}, self.animationcontainer.attrib['name'])
        o2 = xml.SubElement(v, "hkobject")
        set_param(o2, {'name':"name"}, "Resource Data")
        set_param(o2, {'name':"className"}, "hkMemoryResourceContainer")
        set_param(o2, {'name':"variant"}, self.memoryresourcecontainer.attrib['name'])


    def write_animationcontainer(self):
        e = xml.SubElement(self.section, "hkobject")
        self.set_incr_name(e)
        e.set("class", "hkaAnimationContainer")
        self.set_signature(e, "hkaAnimationContainer", [])
        self.animationcontainer = e

    def write_animationcontainer_refs(self):
        e = self.animationcontainer
        set_param(e, {"name":"skeletons", "numelements":"1"}, self.skeleton.attrib['name'])
        set_param(e, {"name":"animations", "numelements":"0"}, "")
        set_param(e, {"name":"bindings", "numelements":"0"}, "")
        set_param(e, {"name":"attachments", "numelements":"0"}, "")
        set_param(e, {"name":"skins", "numelements":"0"}, "")


    def write_parentindices(self, skel, bones):
        set_param(skel, {"name":"parentIndices", "numelements":str(len(bones))},
                  " ".join([str(x) for x in range(-1, len(bones)-1)]))


    def write_bones(self, skel, bones):
        bonesparam = set_param(skel, {"name":"bones", "numelements":str(len(bones))}, "")

        for b in bones:
            obj = xml.SubElement(bonesparam, 'hkobject')
            set_param(obj, {"name":"name"}, b.name)
            set_param(obj, {"name":"lockTranslation"}, "false")


    def write_pose(self, skel, bones):
        
        adjust_mx = Matrix.Rotation(pi/2, 4, Vector([1,0,0]))
        txt = ""
        for b in bones:
            mx = b.matrix_local
            log.debug(f"{b.name} mx before rotation: \n{mx}")
            if b.parent:
                # px = adjust_mx @ b.parent.matrix
                px = b.parent.matrix_local
                mx = px.inverted() @ mx
            log.debug(f"mx after global-to-local: \n{mx}")
            # mx = adjust_mx @ mx
            # log.debug(f"mx after rotation: \n{mx}")
            xl = mx.translation
            xl.rotate(adjust_mx)
            txt += "({0:0.6f} {1:0.6f} {2:0.6f})".format(*xl)
            q = mx.to_quaternion()
            qax = q.axis
            qax.rotate(adjust_mx)
            txt += "({1:0.6f} {2:0.6f} {3:0.6f} {0:0.6f})".format(*q[:])
            s = mx.to_scale()
            txt += "({0:0.6f} {1:0.6f} {2:0.6f})\n".format(*s)

        set_param(skel, {'name':"referencePose", 'numelements':str(len(bones))}, txt)


    def write_skel(self) -> None:
        arma = self.context.object
        bones = [arma.data.bones[x.name] for x in arma.pose.bones if x.bone.select]
        rootbone = self.find_root(bones)
        skel = xml.SubElement(self.section, 'hkobject')
        self.set_incr_name(skel)
        skel.set('class', "hkaSkeleton")
        self.set_signature(skel, "hkaSkeleton", [b.name for b in bones])
        set_param(skel, {"name":"name"}, rootbone.name)
        self.write_parentindices(skel, bones)
        self.write_bones(skel, bones)
        self.write_pose(skel, bones)
        set_param(skel, {"name":"referenceFloats", "numelements":"0"}, "")
        set_param(skel, {"name":"floatSlots", "numelements":"0"}, "")
        set_param(skel, {"name":"localFrames", "numelements":"0"}, "")
        self.skeleton = skel


    def write_memoryresourcecontainer(self):
        e = xml.SubElement(self.section, "hkobject")
        self.set_incr_name(e)
        e.set("class", "hkMemoryResourceContainer")
        self.set_signature(e, "hkMemoryResourceContainer", [])
        set_param(e, {"name":"name"}, "")
        set_param(e, {"name":"resourceHandles", "numelements":"0"}, "")
        set_param(e, {"name":"children", "numelements":"0"}, "")
        self.memoryresourcecontainer = e


    def save(self, filepath=None):
        """Write the XML to a file"""
        self.xmltree.write(filepath if filepath else self.filepath,
                           xml_declaration=True,
                           encoding='utf-8')


    def execute(self, context):
        LogStart(bl_info, "EXPORT SKELETON", "XML")

        self.context = context
        self.write_header()
        self.write_rootlevelcontainer()
        self.write_animationcontainer()
        self.write_skel()
        self.write_memoryresourcecontainer()
        self.write_header_refs()
        self.write_rootlevel_refs()
        self.write_animationcontainer_refs()
        self.save()
        log.info(f"Wrote {self.filepath}")

        status = {'FINISHED'}
        return status
    

    @classmethod
    def poll(cls, context):
        if context.object.mode != 'POSE':
            log.error("Must be in POSE Mode to export skeleton bones")
            return False

        try:
            if len([x for x in context.object.pose.bones if x.bone.select]) == 0:
                log.error("Must select one or more bones in pose mode to export")
                return False
        except:
            log.error("Must have a selected armature with selected bones.")
            return False
        
        return True
    

# -------------------- REGISTER/UNREGISTER --------------------------

def nifly_menu_import_skel(self, context):
    self.layout.operator(ImportSkel.bl_idname, text="Skeleton file (.xml)")
def nifly_menu_export_skel(self, context):
    self.layout.operator(ExportSkel.bl_idname, text="Skeleton file (.xml)")

def unregister():
    try:
        bpy.types.TOPBAR_MT_file_import.remove(nifly_menu_import_skel)
        bpy.types.TOPBAR_MT_file_export.remove(nifly_menu_export_skel)
        bpy.utils.unregister_class(ImportSkel)
        bpy.utils.unregister_class(ExportSkel)
    except:
        pass

def register():
    unregister()
    bpy.types.TOPBAR_MT_file_import.append(nifly_menu_import_skel)
    bpy.types.TOPBAR_MT_file_export.append(nifly_menu_export_skel)
    bpy.utils.register_class(ImportSkel)
    bpy.utils.register_class(ExportSkel)
