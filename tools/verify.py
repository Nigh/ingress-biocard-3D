"""Compile actual SCAD, check STL topology and write generic multipart 3MF.
Run: python3 verify.py [path/to/openscad]. Uses Python standard library only.
"""
from pathlib import Path
import collections
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parent.parent
SCAD = ROOT / "ingress_biocard.scad"
NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def read_stl(path):
    data = path.read_bytes()
    count = struct.unpack_from("<I", data, 80)[0]
    assert len(data) == 84 + 50 * count, "Expected binary STL"
    vertices, triangles, indices = [], [], {}
    for i in range(count):
        xyz = struct.unpack_from("<12fH", data, 84 + 50*i)[3:12]
        face = []
        for j in range(0, 9, 3):
            key = tuple(round(v, 5) for v in xyz[j:j+3])
            if key not in indices:
                indices[key] = len(vertices)
                vertices.append(key)
            face.append(indices[key])
        assert len(set(face)) == 3, "Degenerate triangle"
        triangles.append(face)
    edges, directions = collections.Counter(), collections.Counter()
    volume = 0
    adjacency = [set() for _ in vertices]
    for face in triangles:
        for a, b in zip(face, face[1:]+face[:1]):
            edges[tuple(sorted((a, b)))] += 1
            directions[(a, b)] += 1
            adjacency[a].add(b)
            adjacency[b].add(a)
        a, b, c = (vertices[k] for k in face)
        volume += (a[0]*(b[1]*c[2]-b[2]*c[1]) +
                   a[1]*(b[2]*c[0]-b[0]*c[2]) +
                   a[2]*(b[0]*c[1]-b[1]*c[0])) / 6
    assert all(n == 2 for n in edges.values()), "Open or nonmanifold edges"
    assert all(directions[(a,b)] == directions[(b,a)] for a,b in edges), "Inconsistent winding"
    assert volume > 0, "Empty or inverted mesh"
    unseen, components = set(range(len(vertices))), 0
    while unseen:
        components += 1
        todo = [unseen.pop()]
        while todo:
            for other in adjacency[todo.pop()]:
                if other in unseen:
                    unseen.remove(other)
                    todo.append(other)
    bounds = [[min(v[k] for v in vertices), max(v[k] for v in vertices)] for k in range(3)]
    return vertices, triangles, dict(triangles=count, components=components,
                                     volume_mm3=round(volume, 4), bounds_mm=bounds,
                                     watertight=True, consistent_winding=True)


def compile_stl(executable, target, settings, expect_failure=False, source=SCAD):
    cmd = [executable, "--export-format", "binstl", "-o", str(target)]
    for key, value in settings.items():
        cmd += ["-D", key+"="+json.dumps(value, ensure_ascii=False)]
    result = subprocess.run(cmd+[str(source)], capture_output=True, text=True,
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=180)
    if expect_failure:
        assert "ERROR: Assertion" in result.stderr, result.stderr
        return
    assert result.returncode == 0 and target.exists(), result.stderr
    assert "ERROR:" not in result.stderr and "WARNING:" not in result.stderr, result.stderr
    return read_stl(target)


def write_3mf(path, parts, colors):
    ET.register_namespace("", NS)
    model = ET.Element("{"+NS+"}model", unit="millimeter")
    resources = ET.SubElement(model, "resources")
    materials = ET.SubElement(resources, "basematerials", id="1")
    for name, color in zip(("Body", "Accent"), colors):
        ET.SubElement(materials, "base", name=name, displaycolor=color+"FF")
    for obj_id, (vertices, triangles, _) in enumerate(parts, 2):
        obj = ET.SubElement(resources, "object", id=str(obj_id), type="model",
                            name="Base" if obj_id == 2 else "Accent",
                            pid="1", pindex=str(obj_id-2))
        mesh = ET.SubElement(obj, "mesh")
        verts = ET.SubElement(mesh, "vertices")
        for x,y,z in vertices:
            ET.SubElement(verts, "vertex", x=str(x), y=str(y), z=str(z))
        faces = ET.SubElement(mesh, "triangles")
        for a,b,c in triangles:
            ET.SubElement(faces, "triangle", v1=str(a), v2=str(b), v3=str(c))
    assembly = ET.SubElement(resources, "object", id="4", type="model", name="Biocard")
    components = ET.SubElement(assembly, "components")
    for obj_id in (2, 3):
        ET.SubElement(components, "component", objectid=str(obj_id),
                      transform="1 0 0 0 1 0 0 0 1 0 0 0")
    ET.SubElement(ET.SubElement(model, "build"), "item", objectid="4",
                  transform="1 0 0 0 1 0 0 0 1 0 0 0")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", ET.tostring(model, encoding="utf-8", xml_declaration=True))
        z.writestr("[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        tree = ET.fromstring(z.read("3D/3dmodel.model"))
        assert len(tree.findall(".//{"+NS+"}object")) == 3
        assert len(tree.findall(".//{"+NS+"}component")) == 2


def main():
    executable = sys.argv[1] if len(sys.argv)>1 else shutil.which("openscad")
    assert executable, "Install OpenSCAD or pass its executable path"
    report={"compiler":subprocess.check_output([executable,"--version"],stderr=subprocess.STDOUT,text=True).strip(),
            "models":{},"cases":[],"layout":{},"minimum_feature_mm":0.48*math.cos(math.pi/64)}
    assert report["minimum_feature_mm"]>0.4
    roundtrip_executable=sys.argv[2] if len(sys.argv)>2 else executable
    report["roundtrip_compiler"]=subprocess.check_output([roundtrip_executable,"--version"],stderr=subprocess.STDOUT,text=True).strip()
    products=[]
    fixed_header=None
    for mode in ("Raised","Inset"):
        for faction,color in (("Resistance","#168BD2"),("Enlightened","#39B54A")):
            stem=faction.lower()+("_inset" if mode=="Inset" else "")
            meshes={}
            for part in ("Base","Accent","Assembly"):
                name=stem+"_"+part.lower()
                meshes[part]=compile_stl(executable,ROOT/(name+".stl"),
                    {"faction":faction,"content_mode":mode,"export_part":part})
                report["models"][name]=meshes[part][2]
            assert meshes["Base"][2]["components"]==meshes["Assembly"][2]["components"]==1
            expected_height=1.6 if mode=="Inset" else 2.2
            assert meshes["Assembly"][2]["bounds_mm"]==[[0,63],[0,88],[0,expected_height]]
            assert meshes["Accent"][2]["bounds_mm"][2]==([1,1.6] if mode=="Inset" else [1.6,2.2])
            assert abs(meshes["Base"][2]["volume_mm3"]+meshes["Accent"][2]["volume_mm3"]-
                       meshes["Assembly"][2]["volume_mm3"])<0.03
            vertices=meshes["Accent"][0]
            top=[v for v in vertices if v[1]>70]
            logo=[v for v in top if v[0]<11]
            word=[v for v in top if v[0]>=11]
            assert math.isclose(min(v[0] for v in logo),1,abs_tol=0.002)
            assert math.isclose(max(v[1] for v in logo),87,abs_tol=0.002)
            assert math.isclose(max(v[1] for v in word),87,abs_tol=0.002)
            assert math.isclose(max(v[1] for v in logo)-min(v[1] for v in logo),11.2,abs_tol=0.002)
            assert math.isclose(max(v[1] for v in word)-min(v[1] for v in word),5.6,abs_tol=0.002)
            header_xy={(v[0],v[1]) for v in top}
            if fixed_header is None:fixed_header=header_xy
            else:assert header_xy==fixed_header,"Fixed header changed with faction/mode"
            badge=[v for v in vertices if v[1]<20 and v[0]>50]
            assert math.isclose(min(v[1] for v in badge),1,abs_tol=0.002)
            assert math.isclose(19-max(v[1] for v in badge),1,abs_tol=0.002)
            assert math.isclose(63-max(v[0] for v in badge),1,abs_tol=0.002)
            identity=[v for v in vertices if v[1]<20 and v[0]<50]
            assert abs(min(v[0] for v in identity)-4)<0.01
            assert abs(min(v[1] for v in identity)-(19-max(v[1] for v in identity)))<0.01
            write_3mf(ROOT/(stem+"_multipart.3mf"),[meshes["Base"],meshes["Accent"]],["#F5F1E8",color])
            products.append(stem)
            report["layout"][stem]={"header_left_top_gap_mm":1,"footer_top_bottom_right_gap_mm":1,
                                   "content_mode":mode,"header_height_ratio":0.5,"identity_left_margin_mm":4,
                                   "identity_top_bottom_margin_mm":1}
            print(stem,"production meshes PASS",flush=True)
    with tempfile.TemporaryDirectory() as temp:
        target=Path(temp)/"test.stl";source=Path(temp)/"check.scad"
        def spans(vertices,axis):
            # Extruded mesh components can split dots/counters; merge their ink intervals.
            edges=collections.defaultdict(set)
            for face in triangles:
                for a,b in zip(face,face[1:]+face[:1]):edges[a].add(b);edges[b].add(a)
            unseen=set(range(len(vertices))); intervals=[]
            while unseen:
                todo=[unseen.pop()];component=[]
                while todo:
                    n=todo.pop();component.append(n)
                    for other in edges[n]:
                        if other in unseen:unseen.remove(other);todo.append(other)
                intervals.append([min(vertices[n][axis] for n in component),max(vertices[n][axis] for n in component)])
            merged=[]
            for lo,hi in sorted(intervals):
                if merged and lo<=merged[-1][1]+0.01:merged[-1][1]=max(hi,merged[-1][1])
                else:merged.append([lo,hi])
            return merged
        for gap in (0.5,1.0):
            source.write_text("$fn=64; use <"+str(SCAD)+">; linear_extrude(0.6) label_2d(\"iIljMW\",8.2,"+str(gap)+");")
            vertices,triangles,stats=compile_stl(executable,target,{},source=source)
            intervals=spans(vertices,0)
            assert len(intervals)==6
            assert abs(intervals[0][0])<0.01
            assert intervals[0][1]-intervals[0][0]<2,"i was enlarged"
            gaps=[b[0]-a[1] for a,b in zip(intervals,intervals[1:])]
            assert all(abs(g-gap)<0.02 for g in gaps),gaps
            report["cases"].append({"name":"equal_visible_letter_gap_"+str(gap),"passed":True,"measured_gaps_mm":gaps})
        for settings in ({},{"agentname_spacing":1,"detail_spacing":0.8},{"codename":"gypjÁÉ","location":"gjpqy"},{"bottom_band":24}):
            source.write_text("$fn=64; use <"+str(SCAD)+">; linear_extrude(0.6) identity_2d();")
            vertices,triangles,stats=compile_stl(executable,target,settings,source=source)
            rows=spans(vertices,1);band=settings.get("bottom_band",19)
            assert len(rows)==3
            assert abs(stats["bounds_mm"][0][0]-4)<0.01
            assert abs(rows[0][0]-(band-rows[-1][1]))<0.01
            gaps=[b[0]-a[1] for a,b in zip(rows,rows[1:])]
            assert abs(gaps[0]-gaps[1])<0.01,gaps
            report["cases"].append({"name":"balanced_rows_"+str(settings),"passed":True,"rows_mm":rows,"gaps_mm":gaps})
        for stem in products:
            source.write_text("import("+json.dumps(str(ROOT/(stem+"_multipart.3mf")))+");")
            _,_,stats=compile_stl(roundtrip_executable,target,{},source=source)
            original=report["models"][stem+"_assembly"]
            assert stats["bounds_mm"]==original["bounds_mm"] and stats["components"]==1
            assert abs(stats["volume_mm3"]-original["volume_mm3"])<0.03
            report["cases"].append({"name":stem+"_3mf_roundtrip","passed":True})
        # Full accent area must be supported by the frame; this catches hidden clipping/overhangs.
        for faction in ("Resistance","Enlightened"):
            source.write_text("$fn=64; use <"+str(SCAD)+">; linear_extrude(0.6) intersection(){marks_2d();frame_2d();}")
            _,_,stats=compile_stl(executable,target,{"faction":faction},source=source)
            expected=report["models"][faction.lower()+"_accent"]["volume_mm3"]
            assert abs(stats["volume_mm3"]-expected)<0.03,"Content crosses the opening/frame"
            report["cases"].append({"name":faction.lower()+"_complete_content_supported","passed":True})
        # Echo actual source points, then verify both ramp angles and four minimum frame widths.
        source.write_text("$fn=64; use <"+str(SCAD)+">; echo(window_points());")
        run=subprocess.run([executable,"-o",str(Path(temp)/"window.echo"),str(source)],capture_output=True,text=True,check=True)
        import ast
        points=ast.literal_eval((Path(temp)/"window.echo").read_text().split("ECHO: ")[1].strip())
        for a,b in ((points[7],points[6]),(points[5],points[4])):
            assert abs(math.degrees(math.atan2(b[1]-a[1],b[0]-a[0]))-30)<0.001
        assert points[0][0]==4 and 63-points[1][0]==4 and 88-points[3][1]==4
        assert abs((63+88-6-sum(points[2]))/math.sqrt(2)-4)<0.0001
        logo_support=min(y-math.tan(math.pi/6)*x for x,y in fixed_header if x<11)
        ramp_support=points[7][1]-math.tan(math.pi/6)*points[7][0]
        assert abs((logo_support-ramp_support)*math.cos(math.pi/6)-1)<0.002
        report["window_points_mm"]=points
        report["cases"].append({"name":"30_degree_ramps_equal_minimum_frame_header_clearance","passed":True})
        for name,settings in [
            ("blank_identity",{"codename":"","location":""}),
            ("long_separate_letters",{"codename":"AGENT_WITH_LONG_CODENAME"}),
            ("descenders_accents",{"codename":"gypjÁÉ","location":"gjpqy"}),
            ("italic_font",{"font_name":"Liberation Sans:style=Italic"}),
            ("large_info_band",{"bottom_band":24,"top_right_cut":8}),
            ("minimum_info_band_inset",{"bottom_band":18,"content_mode":"Inset","faction":"Enlightened"}),
        ]:
            vertices,_,stats=compile_stl(executable,target,settings)
            assert stats["components"]==1 and stats["bounds_mm"][0]==[0,63] and stats["bounds_mm"][1]==[0,88]
            if name=="italic_font":
                header={(v[0],v[1]) for v in vertices if v[1]>70 and v[2]>1.6}
                assert header==fixed_header
            report["cases"].append({"name":name,"passed":True})
        for name,settings in [
            ("reject_adjustable_border",{"border_width":5}),
            ("reject_changed_card_size",{"card_width":50}),
            ("reject_bad_layers",{"base_thickness":1.5}),
            ("reject_unknown_mode",{"content_mode":"Other"}),
            ("reject_thin_inset_backing",{"content_mode":"Inset","accent_height":1.2}),
            ("reject_excessive_text",{"codename":"A"*60}),
            ("reject_small_letter_gap",{"agentname_spacing":0.1}),
        ]:
            compile_stl(executable,target,settings,expect_failure=True)
            report["cases"].append({"name":name,"passed":True})
    report["limits"]=["No physical print or slicer toolpath check","No MakerWorld live export test",
                      "Generic 3MF contains parts/materials, not a Bambu print profile",
                      "Print-safe artwork alters fine SVG detail"]
    (ROOT/"validation.json").write_text(json.dumps(report,indent=2)+"\n")
    print("PASS: 12 production meshes, 4 multipart 3MF roundtrips,",len(report["cases"]),"checks")

if __name__=="__main__":main()
