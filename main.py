"""
DGN to KML Converter API
FastAPI server that converts MicroStation DGN files to KML/GeoJSON
using GDAL/OGR library.

Deployed on Render.com (Free Tier).
"""

import os
import re
import uuid
import tempfile
import logging
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from osgeo import ogr, osr
from convert_geojson import convert_dgn_direct_geojson

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="DGN Converter API",
    description="Convert MicroStation DGN files to KML/GeoJSON for web map viewing",
    version="1.0.0",
)

# GZip compression - reduces KML response size by 60-80%
app.add_middleware(GZipMiddleware, minimum_size=500)

# CORS - allow frontend to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Max file size: 15MB
MAX_FILE_SIZE = 15 * 1024 * 1024


# =========================================================================
# TCVN3 (ABC / .VnTime / MstnFont) → Unicode conversion table
# Old Vietnamese fonts (.VnTime, .VnArial, MstnFont160, VNFONTDC, etc.)
# use TCVN3 (TCVN 5712:1993) encoding where bytes 0x80-0xFF map to
# Vietnamese glyphs.
# =========================================================================
TCVN3_TO_UNICODE = {
    # Uppercase base vowels with diacritics
    0xa1: 'Ă', 0xa2: 'Â', 0xa3: 'Ê', 0xa4: 'Ô', 0xa5: 'Ơ', 0xa6: 'Ư', 0xa7: 'Đ',
    # Lowercase base vowels with diacritics
    0xa8: 'ă', 0xa9: 'â', 0xaa: 'ê', 0xab: 'ô', 0xac: 'ơ', 0xad: 'ư', 0xae: 'đ',
    # a with tones: à á ả ã ạ
    0xb5: 'à', 0xb6: 'ả', 0xb7: 'ã', 0xb8: 'á', 0xb9: 'ạ',
    # ă with tones: ằ ẳ ẵ ắ ặ
    0xbb: 'ằ', 0xbc: 'ẳ', 0xbd: 'ẵ', 0xbe: 'ắ', 0xc6: 'ặ',
    # â with tones: ầ ẩ ẫ ấ ậ
    0xc7: 'ầ', 0xc8: 'ẩ', 0xc9: 'ẫ', 0xca: 'ấ', 0xcb: 'ậ',
    # e with tones: è é ẻ ẽ ẹ
    0xcc: 'è', 0xcd: 'ẻ', 0xce: 'ẽ', 0xcf: 'é', 0xd0: 'ẹ',
    # ê with tones: ề ể ễ ế ệ
    0xd2: 'ề', 0xd3: 'ể', 0xd4: 'ễ', 0xd5: 'ế', 0xd6: 'ệ',
    # i with tones: ì ỉ ĩ í ị
    0xd7: 'ì', 0xd8: 'ỉ', 0xdc: 'ĩ', 0xdd: 'í', 0xde: 'ị',
    # o with tones: ò ỏ õ ó ọ
    0xdf: 'ò', 0xe1: 'ỏ', 0xe2: 'õ', 0xe3: 'ó', 0xe4: 'ọ',
    # ô with tones: ồ ổ ỗ ố ộ
    0xe5: 'ồ', 0xe6: 'ổ', 0xe7: 'ỗ', 0xe8: 'ố', 0xe9: 'ộ',
    # ơ with tones: ờ ở ỡ ớ ợ
    0xea: 'ờ', 0xeb: 'ở', 0xec: 'ỡ', 0xed: 'ớ', 0xee: 'ợ',
    # u with tones: ù ủ ũ ú ụ
    0xef: 'ù', 0xf1: 'ủ', 0xf2: 'ũ', 0xf3: 'ú', 0xf4: 'ụ',
    # ư with tones: ừ ử ữ ứ ự
    0xf5: 'ừ', 0xf6: 'ử', 0xf7: 'ữ', 0xf8: 'ứ', 0xf9: 'ự',
    # y with tones: ỳ ỷ ỹ ý ỵ
    0xfa: 'ỳ', 0xfb: 'ỷ', 0xfc: 'ỹ', 0xfd: 'ý', 0xfe: 'ỵ',
}

# Font names that use TCVN3 encoding
TCVN3_FONT_PREFIXES = (
    '.vn', '.Vn', '.VN',
    'Vn', 'VN', 'vn',
    'TCVN', 'tcvn',
    'VNI', 'vni',
    'MstnFont', 'mstnfont',
)


def _is_tcvn3_font(font_name: str) -> bool:
    """Check if a font name is a TCVN3 (ABC) Vietnamese font."""
    if not font_name:
        return False
    fn_clean = font_name.strip().replace(' ', '')
    for prefix in TCVN3_FONT_PREFIXES:
        if fn_clean.startswith(prefix):
            return True
    return False


def _detect_font_from_style(style_str: str) -> str:
    """Extract font name from OGR StyleString.

    Example: LABEL(f:"VnTimeH",t:"text",...)
    """
    if not style_str:
        return ''
    m = re.search(r'LABEL\([^)]*\bf:"([^"]*)"', style_str)
    if m:
        return m.group(1).strip()
    m = re.search(r'LABEL\([^)]*\bf:([^,)]+)', style_str)
    if m:
        return m.group(1).strip()
    return ''


def _fix_text_encoding(text: str, font_name: str = '') -> str:
    """Fix Vietnamese text encoding from DGN V7/V8 and CAD files.

    GDAL's DGN V7 driver reads text as raw bytes. For non-UTF-8 bytes,
    GDAL Python bindings return Python strings with surrogateescape (U+DC00..U+DCFF)
    or Latin-1 code units.

    This function recovers the original byte stream and decodes it using:
    1. TCVN3 (ABC / MstnFont / .VnTime)
    2. UTF-8 misread as Latin-1
    3. Windows-1258
    """
    if not text:
        return text

    # Strip BOM (U+FEFF)
    text = text.lstrip('\ufeff')
    if not text:
        return text

    # Early check: if text already contains valid Vietnamese Unicode chars, do NOT re-encode
    vn_chars = set('àáảãạăắằẳẵặâấầẩẫậèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵđĐĂÂÊÔƠƯ')
    if any(c in vn_chars for c in text):
        return text

    # Decode OGR percent-encoded text (%XX hex sequences)
    if '%' in text:
        try:
            from urllib.parse import unquote
            decoded = unquote(text, encoding='utf-8')
            if decoded != text and any(c in vn_chars for c in decoded):
                return decoded
            decoded_latin = unquote(text, encoding='latin-1')
            if decoded_latin != text:
                text = decoded_latin
        except Exception:
            pass

    # Pure ASCII without high bytes needs no conversion
    if text.isascii():
        return text

    # Extract raw bytes whether string comes with surrogates (\udcxx) or latin-1
    has_surrogates = any(0xdc00 <= ord(c) <= 0xdcff for c in text)
    if has_surrogates:
        raw_bytes = bytes(
            (ord(c) - 0xdc00) if (0xdc00 <= ord(c) <= 0xdcff) else ord(c)
            for c in text
            if ord(c) < 256 or (0xdc00 <= ord(c) <= 0xdcff)
        )
    else:
        try:
            raw_bytes = text.encode('latin-1')
        except (UnicodeDecodeError, UnicodeEncodeError):
            raw_bytes = None

    if raw_bytes is not None:
        # Strategy 1: TCVN3 (ABC) mapping
        tcvn3_count = sum(1 for b in raw_bytes if b in TCVN3_TO_UNICODE)
        if tcvn3_count > 0:
            res = []
            for b in raw_bytes:
                if b in TCVN3_TO_UNICODE:
                    res.append(TCVN3_TO_UNICODE[b])
                else:
                    res.append(chr(b))
            return ''.join(res)

        # Strategy 2: UTF-8 misread as Latin-1
        try:
            fixed = raw_bytes.decode('utf-8')
            if any(c in vn_chars for c in fixed):
                return fixed
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass

        # Strategy 3: CP1258
        try:
            fixed = raw_bytes.decode('cp1258')
            if any(c in vn_chars for c in fixed):
                return fixed
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass

    # Strategy 4: High code units > 0xFF matching TCVN3 directly
    tcvn3_hits = sum(1 for c in text if (ord(c) & 0xFF) in TCVN3_TO_UNICODE)
    if tcvn3_hits > 0:
        res = []
        for c in text:
            b = (ord(c) - 0xdc00) if (0xdc00 <= ord(c) <= 0xdcff) else (ord(c) & 0xFF)
            if b in TCVN3_TO_UNICODE:
                res.append(TCVN3_TO_UNICODE[b])
            else:
                res.append(c)
        return ''.join(res)

    return text


@app.get("/")
def health_check():
    """Health check endpoint - also keeps Render from sleeping."""
    return {"status": "ok", "service": "dgn-converter"}


@app.get("/health")
def health():
    return {"status": "ok", "version": "2.3.0-inspect-v2"}


@app.get("/drivers")
def list_drivers():
    """List all available OGR drivers — useful for debugging DGN support."""
    drivers = []
    for i in range(ogr.GetDriverCount()):
        drv = ogr.GetDriver(i)
        drivers.append(drv.GetName())
    dgn_support = {
        "DGN_V7": "DGN" in drivers,
        "DGN_V8": "DGNV8" in drivers,
    }
    return {"drivers": sorted(drivers), "dgn_support": dgn_support, "total": len(drivers)}


@app.post("/inspect")
async def inspect_dgn(file: UploadFile = File(...)):
    """Inspect DGN text — NO GetStyleString to avoid GDAL segfault."""
    try:
        content = await file.read()
        import tempfile, uuid, os
        tmp_dir = tempfile.mkdtemp()
        ext = os.path.splitext(file.filename or "test.dgn")[1].lower()
        tmp_path = os.path.join(tmp_dir, f"{uuid.uuid4().hex}{ext}")
        with open(tmp_path, "wb") as f:
            f.write(content)
        ds = None
        for dn in ["DGNV8", "DGN", "DXF"]:
            d = ogr.GetDriverByName(dn)
            if d:
                ds = d.Open(tmp_path, 0)
                if ds:
                    break
        if not ds:
            return {"error": "no driver"}
        lyr = ds.GetLayer(0)
        defn = lyr.GetLayerDefn()
        total = lyr.GetFeatureCount()
        fields = [defn.GetFieldDefn(j).GetName() for j in range(defn.GetFieldCount())]
        # Filter for Type=17 (text elements in DGN)
        samples = []
        lyr.SetAttributeFilter("Type = 17")
        lyr.ResetReading()
        feat = lyr.GetNextFeature()
        count = 0
        while feat and count < 10:
            count += 1
            row = {}
            for j in range(defn.GetFieldCount()):
                try:
                    row[defn.GetFieldDefn(j).GetName()] = feat.GetField(j)
                except Exception:
                    pass
            txt = row.get('Text', '')
            if txt:
                try:
                    row['hex'] = txt.encode('latin-1').hex()
                except UnicodeEncodeError:
                    row['codepoints'] = [f"U+{ord(c):04X}" for c in txt[:25]]
            samples.append(row)
            feat = lyr.GetNextFeature()
        # Count Type=17
        type17_count = count
        while feat:
            type17_count += 1
            feat = lyr.GetNextFeature()
        # Also get first 10 features (no filter)
        lyr.SetAttributeFilter(None)
        lyr.ResetReading()
        first10 = []
        feat = lyr.GetNextFeature()
        c = 0
        while feat and c < 10:
            c += 1
            row = {}
            for j in range(defn.GetFieldCount()):
                try:
                    row[defn.GetFieldDefn(j).GetName()] = feat.GetField(j)
                except Exception:
                    pass
            first10.append(row)
            feat = lyr.GetNextFeature()
        ds = None
        try:
            os.remove(tmp_path)
            os.rmdir(tmp_dir)
        except Exception:
            pass
        return {"total": total, "fields": fields, "type17_count": type17_count, "type17_samples": samples, "first10": first10}
    except Exception as e:
        import traceback
        return {"error": str(e), "tb": traceback.format_exc()}




def detect_source_srs(datasource) -> osr.SpatialReference | None:
    """Try to detect the spatial reference from the DGN file."""
    layer = datasource.GetLayer(0)
    if layer is None:
        return None

    srs = layer.GetSpatialRef()
    if srs:
        return srs

    # If no SRS found, return None (caller will handle default)
    return None




def _postprocess_kml(kml_content: str) -> str:
    """Post-process KML to fix polygon styling and point labels from DGN conversion.

    - Remove opaque polygon fills (set to transparent)
    - Fix <name> for text-point Placemarks: extract from ExtendedData when name is a
      raw DGN type number (e.g. '17') or empty
    - Add default style with outline-only polygons
    """
    # Fix Point Placemark names: extract actual text label from ExtendedData
    # GDAL DGN driver stores the text in fields like EntityNum/Text/TextString
    # but KML <name> gets the element type code (e.g., '17' for text elements)
    kml_content = _fix_point_labels(kml_content)

    # Remove all existing PolyStyle blocks (they contain opaque fills from DGN)
    kml_content = re.sub(
        r'<PolyStyle>.*?</PolyStyle>',
        '<PolyStyle><fill>0</fill><outline>1</outline></PolyStyle>',
        kml_content,
        flags=re.DOTALL,
    )

    # If there are Style blocks with color fills, make polygon colors transparent
    kml_content = re.sub(
        r'(<PolyStyle>\s*<color>)[0-9a-fA-F]{8}(</color>)',
        r'\g<1>00000000\2',
        kml_content,
    )

    # Inject a default shared style at the top of Document for clean outlines
    default_style = """
    <Style id="dgn_outline">
      <LineStyle>
        <color>ff0000ff</color>
        <width>1.5</width>
      </LineStyle>
      <PolyStyle>
        <fill>0</fill>
        <outline>1</outline>
      </PolyStyle>
    </Style>"""

    kml_content = kml_content.replace('<Document>', f'<Document>{default_style}', 1)

    logger.info("KML post-processing: fixed labels + removed opaque fills + added outline style")
    return kml_content


def _fix_point_labels(kml_content: str) -> str:
    """Fix <name> for Point Placemarks with numeric-only names. Optimized for large files."""
    TEXT_FIELD_NAMES = ['Text', 'EntityNum', 'TextString', 'Label', 'Name']

    # Fast path: if no Point placemarks exist, skip entirely
    if '<Point>' not in kml_content:
        return kml_content

    parts = []
    search_from = 0
    pm_start_tag = '<Placemark>'
    pm_end_tag = '</Placemark>'

    while True:
        start = kml_content.find(pm_start_tag, search_from)
        if start == -1:
            parts.append(kml_content[search_from:])
            break

        # Add content before this Placemark
        parts.append(kml_content[search_from:start])

        end = kml_content.find(pm_end_tag, start)
        if end == -1:
            parts.append(kml_content[start:])
            break
        end += len(pm_end_tag)
        block = kml_content[start:end]
        search_from = end

        # Skip non-Point placemarks
        if '<Point>' not in block:
            parts.append(block)
            continue

        # Extract current <name>
        name_s = block.find('<name>')
        name_e = block.find('</name>')
        current_name = ''
        if name_s >= 0 and name_e > name_s:
            current_name = block[name_s + 6:name_e].strip()

        # If name is already meaningful, keep as-is
        if current_name and not current_name.isdigit():
            parts.append(block)
            continue

        # Try to extract text from SimpleData fields
        extracted = ''
        for fn in TEXT_FIELD_NAMES:
            tag = f'<SimpleData name="{fn}">'
            idx = block.find(tag)
            if idx >= 0:
                val_start = idx + len(tag)
                val_end = block.find('</SimpleData>', val_start)
                if val_end > val_start:
                    val = block[val_start:val_end].strip()
                    if val and not val.isdigit():
                        extracted = val
                        break
                    elif val and val.isdigit() and len(val) >= 3:
                        extracted = val
                        break

        if not extracted:
            # Remove pointless Point placemark
            continue

        extracted = _fix_text_encoding(extracted)

        # Insert/replace <name>
        if name_s >= 0 and name_e > name_s:
            block = block[:name_s] + f'<name>{extracted}</name>' + block[name_e + 7:]
        else:
            block = block.replace('<Placemark>', f'<Placemark><name>{extracted}</name>', 1)

        parts.append(block)

    return ''.join(parts)




def _polygon_to_linestring(geom):
    """Convert a polygon geometry to its boundary linestring(s).

    This eliminates polygon fills entirely — only the outline remains.
    Works for both Polygon and MultiPolygon.
    """
    geom_type = geom.GetGeometryType()
    rings = []

    if geom_type in (ogr.wkbPolygon, ogr.wkbPolygon25D):
        for ring_idx in range(geom.GetGeometryCount()):
            ring = geom.GetGeometryRef(ring_idx)
            if ring and ring.GetPointCount() > 1:
                line = ogr.Geometry(ogr.wkbLineString)
                for pt_idx in range(ring.GetPointCount()):
                    line.AddPoint(*ring.GetPoint(pt_idx))
                rings.append(line)

    elif geom_type in (ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D):
        for poly_idx in range(geom.GetGeometryCount()):
            poly = geom.GetGeometryRef(poly_idx)
            if poly:
                sub = _polygon_to_linestring(poly)
                if sub:
                    if sub.GetGeometryType() == ogr.wkbLineString:
                        rings.append(sub)
                    else:
                        for k in range(sub.GetGeometryCount()):
                            rings.append(sub.GetGeometryRef(k).Clone())

    if not rings:
        return None
    if len(rings) == 1:
        return rings[0]

    multi = ogr.Geometry(ogr.wkbMultiLineString)
    for r in rings:
        multi.AddGeometry(r)
    return multi


def _format_cadastral_label(labels: list) -> str:
    """Format clustered DGN text labels into single-line cadastral notation.

    Output: "CODE parcel/area" (e.g., "ONT+HNK 53/434,0")
    """
    if len(labels) == 1:
        return labels[0]

    codes = []
    numbers = []
    for label in labels:
        label = label.strip()
        if not label:
            continue
        clean = label.replace(',', '.')
        try:
            num_val = float(clean)
            numbers.append((num_val, label))
        except ValueError:
            codes.append(label)

    if codes and len(numbers) >= 2:
        code_str = '+'.join(codes)
        return f"{code_str} {numbers[0][1]}/{numbers[1][1]}"

    if codes and len(numbers) == 1:
        return f"{'+'.join(codes)} {numbers[0][1]}"

    if not codes and len(numbers) >= 2:
        return f"{numbers[0][1]}/{numbers[1][1]}"

    return ' '.join(labels)


def _cluster_text_points(
    text_points: list,
    threshold: float = 0.00012,
) -> list:
    """Cluster nearby text points into merged cadastral labels.

    Grouping is strictly per-level:
    - Cadastral plot label layers (Level 5 or 33) are clustered so that
      [crop_type, parcel_num, area] become a single cadastral label: "CODE parcel/area".
    - Other layers (Level 9 boundary dimensions, Level 13 surrounding plots,
      Level 28 road/landmark labels, Level 61 notes, etc.) are kept as INDEPENDENT
      labels with exact locations, avoiding cross-layer corruption.
    """
    if not text_points:
        return []

    by_level: dict = {}
    for pt in text_points:
        lvl = pt.get('level')
        try:
            lvl_key = int(lvl) if lvl is not None else 0
        except (ValueError, TypeError):
            lvl_key = str(lvl) if lvl is not None else 0
        by_level.setdefault(lvl_key, []).append(pt)

    CADASTRAL_LABEL_LEVELS = {3, 5, 8, 10, 11, 12, 13, 33}
    results: list = []
    for lvl_key, pts in by_level.items():
        # Cadastral plot label levels: Level 13 (standard Famis), Level 5, 33 (gCadas/ViLIS), etc.
        # Or if the entire file has only 1 level
        is_cadastral_lvl = (lvl_key in CADASTRAL_LABEL_LEVELS) or (len(by_level) == 1)
        if is_cadastral_lvl and len(pts) > 1:
            cell_size = threshold
            grid: dict = {}
            for idx, pt in enumerate(pts):
                cx = int(pt['x'] / cell_size)
                cy = int(pt['y'] / cell_size)
                grid.setdefault((cx, cy), []).append(idx)

            used = [False] * len(pts)
            for idx in range(len(pts)):
                if used[idx]:
                    continue
                used[idx] = True
                queue = [idx]
                c_indices = [idx]
                head = 0

                while head < len(queue) and len(c_indices) < 5:
                    ci = queue[head]
                    head += 1
                    cpt = pts[ci]
                    cx = int(cpt['x'] / cell_size)
                    cy = int(cpt['y'] / cell_size)

                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            ckey = (cx + dx, cy + dy)
                            if ckey not in grid:
                                continue
                            for j in grid[ckey]:
                                if used[j] or len(c_indices) >= 5:
                                    continue
                                jpt = pts[j]
                                if abs(jpt['x'] - cpt['x']) < threshold and abs(jpt['y'] - cpt['y']) < threshold:
                                    used[j] = True
                                    queue.append(j)
                                    c_indices.append(j)

                cluster = [pts[i] for i in c_indices]
                if len(cluster) == 1:
                    results.append(cluster[0])
                    continue

                codes = []
                numbers = []
                for cp in cluster:
                    lbl = cp['label'].strip()
                    if not lbl:
                        continue
                    clean_num = lbl.replace(',', '.')
                    sub_m = re.match(r'^(\d+)(?:[a-zA-Z]|-[\da-zA-Z]+)?$', clean_num)
                    try:
                        val = float(clean_num)
                        numbers.append((val, lbl, cp))
                    except ValueError:
                        if sub_m:
                            val = float(sub_m.group(1))
                            numbers.append((val, lbl, cp))
                        else:
                            codes.append(lbl)

                # In MicroStation cadastral maps, parcel number is placed above area (higher Y)
                numbers.sort(key=lambda item: -item[2]['y'])

                codes_unique = list(dict.fromkeys(codes))
                code_str = '+'.join(codes_unique) if codes_unique else ''
                if len(numbers) >= 2:
                    parcel = numbers[0][1]
                    area = numbers[1][1]
                    label = f"{code_str} {parcel}/{area}".strip()
                elif len(numbers) == 1:
                    label = f"{code_str} {numbers[0][1]}".strip()
                else:
                    label = ' '.join(codes_unique)

                cx = sum(c['x'] for c in cluster) / len(cluster)
                cy = sum(c['y'] for c in cluster) / len(cluster)
                results.append({'x': cx, 'y': cy, 'label': label, 'level': lvl_key})
        else:
            # All other levels stay independent (dimensions, neighbor plots, roads, titles)
            for pt in pts:
                results.append(pt)

    return results



def convert_dgn_to_format(
    input_path: str,
    output_format: str = "KML",
    source_epsg: int | None = None,
    central_meridian: float | None = None,
) -> bytes:
    """
    Convert a DGN/DXF file to KML or GeoJSON using OGR.

    Args:
        input_path: Path to input file
        output_format: 'KML' or 'GeoJSON'
        source_epsg: EPSG code of source coordinate system
        central_meridian: Central meridian (lon_0) for VN2000 provincial system

    Returns:
        Converted file content as bytes
    """
    # Try to open with explicit drivers
    src_ds = None
    tried_drivers = []
    file_ext = os.path.splitext(input_path)[1].lower()

    if file_ext in ('.dgn',):
        # Check DGN file format by reading magic bytes
        file_format_info = ""
        try:
            with open(input_path, "rb") as f:
                header = f.read(16)
            if len(header) >= 4:
                # DGN V8 files start with OLE2 container: D0 CF 11 E0
                if header[:4] == b'\xd0\xcf\x11\xe0':
                    file_format_info = "DGN V8 (OLE2 container detected)"
                else:
                    file_format_info = f"DGN V7 (header: {header[:8].hex()})"
            logger.info(f"File format detection: {file_format_info}")
        except Exception as e:
            logger.warning(f"Could not read file header: {e}")

        # Try DGN V8 driver first, then V7
        for driver_name_try in ["DGNV8", "DGN"]:
            drv = ogr.GetDriverByName(driver_name_try)
            if drv is not None:
                tried_drivers.append(driver_name_try)
                src_ds = drv.Open(input_path, 0)
                if src_ds is not None:
                    logger.info(f"Opened with driver: {driver_name_try}")
                    break
    elif file_ext in ('.dxf',):
        drv = ogr.GetDriverByName("DXF")
        if drv:
            tried_drivers.append("DXF")
            src_ds = drv.Open(input_path, 0)
    elif file_ext in ('.dwg',):
        # Try CAD driver (uses OpenCAD library) and DWG driver
        for driver_name_try in ["CAD", "DWG"]:
            drv = ogr.GetDriverByName(driver_name_try)
            if drv is not None:
                tried_drivers.append(driver_name_try)
                src_ds = drv.Open(input_path, 0)
                if src_ds is not None:
                    logger.info(f"Opened DWG with driver: {driver_name_try}")
                    break

        if src_ds is None and file_ext == '.dwg':
            raise ValueError(
                f"File DWG phiên bản mới không được hỗ trợ trực tiếp. "
                f"Vui lòng mở file trong AutoCAD/MicroStation → File → Save As → "
                f"chọn định dạng DXF và tải lên lại."
            )

    # Fallback: let OGR auto-detect
    if src_ds is None:
        tried_drivers.append("auto-detect")
        src_ds = ogr.Open(input_path, 0)

    if src_ds is None:
        available = ", ".join(tried_drivers)
        fmt_msg = f" Detected format: {file_format_info}." if file_ext == '.dgn' and file_format_info else ""

        if file_ext == '.dgn' and 'V8' in (file_format_info or ''):
            raise ValueError(
                f"File DGN V8 không được hỗ trợ trực tiếp. "
                f"Vui lòng mở file trong MicroStation → File → Save As → "
                f"chọn định dạng DXF hoặc DGN V7 → upload lại file mới."
            )
        raise ValueError(
            f"Cannot open file.{fmt_msg} Tried drivers: [{available}]."
        )

    layer_count = src_ds.GetLayerCount()
    if layer_count == 0:
        raise ValueError("DGN file contains no layers.")

    logger.info(f"DGN file has {layer_count} layer(s)")

    # Determine output driver and extension
    if output_format.upper() == "KML":
        driver_name = "KML"
        ext = ".kml"
    elif output_format.upper() == "GEOJSON":
        driver_name = "GeoJSON"
        ext = ".geojson"
    else:
        raise ValueError(f"Unsupported output format: {output_format}")

    out_driver = ogr.GetDriverByName(driver_name)
    if out_driver is None:
        raise RuntimeError(f"OGR driver '{driver_name}' not available")

    # Create output file
    base_name = os.path.splitext(input_path)[0]
    output_path = base_name + ext
    if os.path.exists(output_path):
        os.remove(output_path)

    out_ds = out_driver.CreateDataSource(output_path)
    if out_ds is None:
        raise RuntimeError("Failed to create output datasource")

    # Setup coordinate transformation
    target_srs = osr.SpatialReference()
    target_srs.ImportFromEPSG(4326)  # WGS84 (what KML/web maps use)
    target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    coord_transform = None
    if central_meridian:
        # Build VN2000 provincial projection from central meridian
        vn2000_proj = (
            f'+proj=tmerc +lat_0=0 +lon_0={central_meridian} +k=0.9999 '
            f'+x_0=500000 +y_0=0 +ellps=WGS84 '
            f'+towgs84=-191.90441429,-39.30318279,-111.45032835,'
            f'-0.00928836,0.01975479,-0.00427372,0.252906278 '
            f'+units=m +no_defs'
        )
        source_srs = osr.SpatialReference()
        source_srs.ImportFromProj4(vn2000_proj)
        source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        coord_transform = osr.CoordinateTransformation(source_srs, target_srs)
        logger.info(f"Using VN2000 with central meridian: {central_meridian}")
    elif source_epsg:
        source_srs = osr.SpatialReference()
        source_srs.ImportFromEPSG(source_epsg)
        source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        coord_transform = osr.CoordinateTransformation(source_srs, target_srs)
    else:
        # Try to detect from file
        detected_srs = detect_source_srs(src_ds)
        if detected_srs:
            detected_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            coord_transform = osr.CoordinateTransformation(detected_srs, target_srs)
        else:
            layer0 = src_ds.GetLayer(0) if src_ds else None
            if layer0:
                try:
                    ext = layer0.GetExtent()
                    if ext and max(abs(ext[0]), abs(ext[1]), abs(ext[2]), abs(ext[3])) > 1000:
                        northing = (ext[2] + ext[3]) / 2.0
                        if northing < 800000:
                            northing = (ext[0] + ext[1]) / 2.0
                        approx_lat = northing / 110574.0
                        if 11.0 <= approx_lat <= 12.5:
                            auto_ktt = 108.25
                        elif 8.5 <= approx_lat < 11.0:
                            auto_ktt = 105.75
                        elif 12.5 < approx_lat <= 16.5:
                            auto_ktt = 108.00
                        elif 16.5 < approx_lat <= 19.0:
                            auto_ktt = 106.50
                        elif 19.0 < approx_lat <= 23.5:
                            auto_ktt = 105.00
                        else:
                            auto_ktt = 108.25

                        vn2000_proj = (
                            f'+proj=tmerc +lat_0=0 +lon_0={auto_ktt} +k=0.9999 '
                            f'+x_0=500000 +y_0=0 +ellps=WGS84 '
                            f'+towgs84=-191.90441429,-39.30318279,-111.45032835,'
                            f'-0.00928836,0.01975479,-0.00427372,0.252906278 '
                            f'+units=m +no_defs'
                        )
                        source_srs = osr.SpatialReference()
                        source_srs.ImportFromProj4(vn2000_proj)
                        source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                        coord_transform = osr.CoordinateTransformation(source_srs, target_srs)
                        logger.info(f"[AUTO-DETECT] Extent={ext} -> lat={approx_lat:.2f}° -> VN2000 KTT={auto_ktt}°")
                except Exception as ex:
                    logger.warning(f"Failed to auto-detect VN2000 projection: {ex}")

    total_features = 0
    skipped = 0
    total_points = 0
    total_buffered_points = 0
    # Max point labels to avoid crashing browser (DGN cadastral files can have 50k+ points)
    MAX_BUFFERED_POINTS = 50000
    MAX_POINT_LABELS = 25000

    # Polygon types that should be converted to linestrings (to avoid fills)
    POLYGON_TYPES = {
        ogr.wkbPolygon, ogr.wkbPolygon25D,
        ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D,
    }
    POINT_TYPES = {
        ogr.wkbPoint, ogr.wkbPoint25D,
        ogr.wkbMultiPoint, ogr.wkbMultiPoint25D,
    }

    # Common text field names for DGN and DXF
    # DGN: EntityNum contains the text annotation
    # DXF: Text field for TEXT entities, SubClasses may contain MTEXT content
    TEXT_FIELDS = ['Text', 'TEXT', 'text', 'EntityNum', 'TextString',
                   'Label', 'LABEL', 'Feature_Code', 'Description',
                   'Name', 'NAME', 'SubClasses', 'RawCodeValues']

    # Detect file format for format-specific handling
    _is_dxf = file_ext in ('.dxf',)

    for i in range(layer_count):
        src_layer = src_ds.GetLayer(i)
        if src_layer is None:
            continue

        layer_name = src_layer.GetName() or f"Layer_{i}"
        feature_count = src_layer.GetFeatureCount()
        logger.info(f"Processing layer '{layer_name}' with {feature_count} features")

        src_defn_dbg = src_layer.GetLayerDefn()

        # Create output layer - force to generic geometry so mixed types work
        out_layer = out_ds.CreateLayer(
            layer_name,
            srs=target_srs,
            geom_type=ogr.wkbUnknown,
        )

        # Copy field definitions from source layer
        src_defn = src_layer.GetLayerDefn()
        for j in range(src_defn.GetFieldCount()):
            field_defn = src_defn.GetFieldDefn(j)
            out_layer.CreateField(field_defn)

        # Add a 'Name' field — GDAL KML driver maps the 'Name' field to <name>
        # We populate it with the actual text extracted from StyleString for point features
        name_field_already_exists = src_defn.GetFieldIndex('Name') >= 0
        if not name_field_already_exists:
            out_layer.CreateField(ogr.FieldDefn('Name', ogr.OFTString))
        out_defn = out_layer.GetLayerDefn()
        name_out_idx = out_defn.GetFieldIndex('Name')

        src_layer.ResetReading()
        feature = src_layer.GetNextFeature()

        # Buffer for text point features — will be clustered before output
        text_point_buffer = []  # list of {'x': float, 'y': float, 'label': str}
        _font_logged = False  # Log detected font once per layer

        # Cache level field index (avoid repeated lookup per feature)
        SKIP_LEVELS_INT = {3, 13}
        SKIP_LAYER_NAMES = {'3', '13'}  # String match for DXF Layer field
        _level_idx = -1
        _src_defn = src_layer.GetLayerDefn()

        all_fields = [_src_defn.GetFieldDefn(fi).GetName() for fi in range(_src_defn.GetFieldCount())]

        for lvl_name in ('Level', 'level', 'LEVEL', 'Layer', 'layer', 'LAYER'):
            _level_idx = _src_defn.GetFieldIndex(lvl_name)
            if _level_idx >= 0:
                logger.info(f"[LEVEL] Found level field '{lvl_name}' at index {_level_idx}, will skip levels: {SKIP_LEVELS_INT}")
                break
        if _level_idx < 0:
            logger.warning(f"[LEVEL] No Level/Layer field found in layer '{layer_name}' — cannot filter by level. Available: {all_fields}")

        _level_skip_count = 0

        while feature is not None:
            geom = feature.GetGeometryRef()
            current_text_label = ''

            # Level/Layer skip — only skip LINE and POLYGON features
            # KEEP point/text features (they contain parcel labels: crop type, number, area)
            if _level_idx >= 0 and geom is not None:
                lv = feature.GetField(_level_idx)
                geom_type_check = geom.GetGeometryType()
                is_point_feature = geom_type_check in POINT_TYPES

                if lv is not None and not is_point_feature:
                    # Try integer comparison first, then string match
                    should_skip = False
                    try:
                        lv_int = int(lv) if not isinstance(lv, int) else lv
                        if lv_int in SKIP_LEVELS_INT:
                            should_skip = True
                    except (ValueError, TypeError):
                        lv_str = str(lv).strip()
                        if lv_str in SKIP_LAYER_NAMES:
                            should_skip = True

                    if should_skip:
                        if _level_skip_count < 5:
                            logger.info(f"[LEVEL] Skipping LINE/POLY at Level/Layer '{lv}' (geom={geom_type_check})")
                        _level_skip_count += 1
                        feature = src_layer.GetNextFeature()
                        skipped += 1
                        continue

            if geom is not None:
                geom_type = geom.GetGeometryType()

                # For point features: only keep if there is meaningful text label
                if geom_type in POINT_TYPES:
                    if total_buffered_points >= MAX_BUFFERED_POINTS:
                        feature = src_layer.GetNextFeature()
                        skipped += 1
                        continue

                    current_text_label = ''
                    src_defn_scan = src_layer.GetLayerDefn()

                    # DXF debug: log first 5 point features to see what fields are available
                    if _is_dxf and total_points < 5:
                        debug_fields = {}
                        for fi in range(src_defn_scan.GetFieldCount()):
                            fn = src_defn_scan.GetFieldDefn(fi).GetName()
                            try:
                                fv = feature.GetFieldAsString(fi)
                                if fv:
                                    debug_fields[fn] = fv[:80]
                            except Exception:
                                pass
                        ss = feature.GetStyleString() or ''
                        logger.info(f"[DXF-DEBUG] Point #{total_points}: style='{ss[:120]}' fields={debug_fields}")

                    # PRIMARY: Extract from OGR StyleString
                    style_str = feature.GetStyleString() or ''
                    detected_font = ''
                    if style_str:
                        # Detect font for encoding
                        detected_font = _detect_font_from_style(style_str)
                        if detected_font and not _font_logged:
                            is_tcvn = _is_tcvn3_font(detected_font)
                            logger.info(f"[FONT] Detected font: '{detected_font}' → TCVN3={is_tcvn}")
                            _font_logged = True

                        m = re.search(r'LABEL\([^)]*\bt:"([^"]*)"', style_str)
                        if not m:
                            m = re.search(r'LABEL\([^)]*\bt:([^,)]+)', style_str)
                        if m:
                            current_text_label = m.group(1).strip()

                    # FALLBACK: scan fields with GetFieldAsString()
                    if not current_text_label:
                        for tf in TEXT_FIELDS:
                            idx = src_defn_scan.GetFieldIndex(tf)
                            if idx >= 0:
                                try:
                                    val = feature.GetFieldAsString(idx).strip()
                                    if val and val != '0':
                                        current_text_label = val
                                        break
                                except Exception:
                                    pass

                    # Fix Vietnamese text encoding (font-aware: TCVN3 / UTF-8 / CP1258)
                    current_text_label = _fix_text_encoding(current_text_label, detected_font)

                    # Skip if no label
                    if not current_text_label or not current_text_label.strip():
                        feature = src_layer.GetNextFeature()
                        skipped += 1
                        continue

                    total_buffered_points += 1

                # For point features: buffer them for clustering instead of writing directly
                if geom_type in POINT_TYPES and current_text_label:
                    pt_geom = geom.Clone()
                    if coord_transform:
                        pt_geom.Transform(coord_transform)
                    x = pt_geom.GetX()
                    y = pt_geom.GetY()
                    pt_level = feature.GetField(_level_idx) if _level_idx >= 0 else None
                    text_point_buffer.append({'x': x, 'y': y, 'label': current_text_label, 'level': pt_level})
                    feature = src_layer.GetNextFeature()
                    continue

                if coord_transform:
                    geom.Transform(coord_transform)

                # DXF: Check non-point features for text labels (TEXT/MTEXT with non-point geom)
                if _is_dxf and geom_type not in POINT_TYPES and total_buffered_points < MAX_BUFFERED_POINTS:
                    dxf_text = ''
                    dxf_style = feature.GetStyleString() or ''
                    if dxf_style and 'LABEL' in dxf_style:
                        m = re.search(r'LABEL\([^)]*\bt:"([^"]*)"', dxf_style)
                        if not m:
                            m = re.search(r'LABEL\([^)]*\bt:([^,)]+)', dxf_style)
                        if m:
                            dxf_text = m.group(1).strip()

                    # Also check Text field directly
                    if not dxf_text:
                        _dxf_defn = src_layer.GetLayerDefn()
                        for tf in ('Text', 'TEXT', 'text'):
                            tidx = _dxf_defn.GetFieldIndex(tf)
                            if tidx >= 0:
                                try:
                                    tv = feature.GetFieldAsString(tidx).strip()
                                    if tv and tv != '0':
                                        dxf_text = tv
                                        break
                                except Exception:
                                    pass

                    if dxf_text:
                        # Fix encoding
                        dxf_font = _detect_font_from_style(dxf_style) if dxf_style else ''
                        dxf_text = _fix_text_encoding(dxf_text, dxf_font)

                        if dxf_text.strip():
                            # Extract centroid for the label position
                            centroid = geom.Centroid()
                            if centroid:
                                dxf_level = feature.GetField(_level_idx) if _level_idx >= 0 else None
                                text_point_buffer.append({
                                    'x': centroid.GetX(),
                                    'y': centroid.GetY(),
                                    'label': dxf_text,
                                    'level': dxf_level,
                                })
                                total_buffered_points += 1
                                if total_points <= 3:
                                    logger.info(f"[DXF-TEXT] Extracted text from non-point (type={geom_type}): '{dxf_text[:50]}'")
                                # Don't skip — still output the geometry as line/polygon
                                # The text label is added separately via clustering

                # Convert polygons to their boundary linestrings (no fill!)
                if geom_type in POLYGON_TYPES:
                    boundary_geom = _polygon_to_linestring(geom)
                    if boundary_geom is None:
                        feature = src_layer.GetNextFeature()
                        continue
                    out_feature = ogr.Feature(out_layer.GetLayerDefn())
                    out_feature.SetGeometry(boundary_geom)
                else:
                    out_feature = ogr.Feature(out_layer.GetLayerDefn())
                    out_feature.SetGeometry(geom)

                # Copy only essential fields (skip heavy copy for performance)
                # For KML output, the key fields are geometry + Name
                out_layer.CreateFeature(out_feature)
                total_features += 1

            feature = src_layer.GetNextFeature()

        # Cluster buffered text points and write merged labels
        if text_point_buffer:
            clustered = _cluster_text_points(text_point_buffer)
            logger.info(f"Clustered {len(text_point_buffer)} text points → {len(clustered)} merged labels")
            for cp in clustered:
                if total_points >= MAX_POINT_LABELS:
                    skipped += 1
                    continue
                try:
                    pt_geom = ogr.Geometry(ogr.wkbPoint)
                    pt_geom.AddPoint(float(cp['x']), float(cp['y']), 0.0)
                    out_feature = ogr.Feature(out_layer.GetLayerDefn())
                    out_feature.SetGeometry(pt_geom)
                    if name_out_idx >= 0:
                        out_feature.SetField(name_out_idx, str(cp['label']))
                    out_layer.CreateFeature(out_feature)
                    total_features += 1
                    total_points += 1
                except Exception as e:
                    logger.warning(f"Failed to write clustered point: {e}")
                    continue

    # Cleanup OGR datasets
    out_ds = None
    src_ds = None

    logger.info(f"Converted {total_features} features ({total_points} point labels), skipped {skipped} features (level-filtered: {_level_skip_count})")

    if total_features == 0:
        raise ValueError("No geometry features found in DGN file.")

    # Read output and post-process KML to fix polygon styling
    with open(output_path, "r", encoding="utf-8") as f:
        content = f.read()

    if output_format.upper() == "KML":
        content = _postprocess_kml(content)

    result = content.encode("utf-8")

    # Cleanup temp output
    try:
        os.remove(output_path)
    except Exception:
        pass

    return result


@app.post("/convert")
async def convert_dgn(
    file: UploadFile = File(..., description="DGN/DXF/DWG file to convert"),
    format: str = Query("KML", description="Output format: KML or GeoJSON"),
    source_epsg: int | None = Query(None, description="Source EPSG code (e.g., 9210 for VN2000 Mui 6)"),
    central_meridian: float | None = Query(None, description="Central meridian for VN2000 provincial system (e.g., 108.25 for Ninh Thuan)"),
):
    """
    Convert a DGN file to KML or GeoJSON.

    - Upload a .dgn file
    - Optionally specify the source coordinate system (EPSG code)
    - Returns the converted KML or GeoJSON file
    """
    # Validate file extension
    if not file.filename:
        raise HTTPException(400, "No filename provided")

    allowed_ext = (".dgn", ".dxf", ".dwg")
    if not file.filename.lower().endswith(allowed_ext):
        raise HTTPException(400, f"Only {', '.join(allowed_ext)} files are accepted")

    # Read file content
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(413, f"File too large. Max size: {MAX_FILE_SIZE // (1024*1024)}MB")

    if len(content) == 0:
        raise HTTPException(400, "Empty file")

    # Save to temp file (keep original extension for driver auto-detection)
    file_ext = os.path.splitext(file.filename)[1].lower()
    tmp_dir = tempfile.mkdtemp()
    input_path = os.path.join(tmp_dir, f"{uuid.uuid4().hex}{file_ext}")

    try:
        with open(input_path, "wb") as f:
            f.write(content)

        logger.info(f"Converting {file.filename} ({len(content)} bytes) to {format}")

        if format.upper() == "GEOJSON":
            # Fast path: direct in-memory GeoJSON (no file I/O, no regex)
            helpers = {
                'fix_text_encoding': _fix_text_encoding,
                'detect_font_from_style': _detect_font_from_style,
                'is_tcvn3_font': _is_tcvn3_font,
                'polygon_to_linestring': _polygon_to_linestring,
                'cluster_text_points': _cluster_text_points,
            }
            result = convert_dgn_direct_geojson(
                input_path, source_epsg, central_meridian, _helpers=helpers
            )
        else:
            # Legacy KML path
            result = convert_dgn_to_format(input_path, format, source_epsg, central_meridian)

        # Determine content type
        if format.upper() == "KML":
            media_type = "application/vnd.google-earth.kml+xml"
            out_ext = ".kml"
        else:
            media_type = "application/geo+json"
            out_ext = ".geojson"

        out_filename = file.filename.rsplit(".", 1)[0] + out_ext

        return Response(
            content=result,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{out_filename}"',
                "X-Original-Filename": file.filename,
                "X-Output-Format": format.upper(),
            },
        )

    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Conversion failed: {e}\n{tb}")
        raise HTTPException(500, f"Conversion failed: {str(e)}\n\nTraceback:\n{tb[-500:]}")
    finally:
        # Cleanup
        try:
            os.remove(input_path)
            os.rmdir(tmp_dir)
        except Exception:
            pass


# Common VN2000 EPSG codes for reference
VN2000_CODES = {
    "VN2000 / TM-3 zone 481 (Mui 3, KTT 104.75)": 9205,
    "VN2000 / TM-3 zone 482 (Mui 3, KTT 105.75)": 9206,
    "VN2000 / TM-3 zone 491 (Mui 3, KTT 106.25)": 9207,
    "VN2000 / UTM zone 48N (Mui 6)": 9209,
    "VN2000 / UTM zone 49N (Mui 6)": 9210,
}


@app.get("/epsg-codes")
def list_epsg_codes():
    """List common VN2000 EPSG codes for coordinate transformation."""
    return {"codes": VN2000_CODES}
