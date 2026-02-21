#include "mesh_loader.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

/* -------------------------------------------------------------------------
 * Internal helpers
 * ---------------------------------------------------------------------- */

/* Return pointer to the file extension (the '.' character) or NULL. */
static const char *_file_ext(const char *path)
{
    const char *dot = NULL;
    const char *p   = path;
    while (*p) {
        if (*p == '.') dot = p;
        ++p;
    }
    return dot;
}

/* Case-insensitive comparison of two short strings. */
static int _stricmp_ascii(const char *a, const char *b)
{
    while (*a && *b) {
        if (tolower((unsigned char)*a) != tolower((unsigned char)*b))
            return 1;
        ++a; ++b;
    }
    return (*a != *b) ? 1 : 0;
}

/* -------------------------------------------------------------------------
 * Binary STL loader
 * ---------------------------------------------------------------------- */

static CoreMesh _load_stl(const char *path)
{
    CoreMesh mesh;
    memset(&mesh, 0, sizeof(CoreMesh));

    FILE *fp = fopen(path, "rb");
    if (!fp) return mesh;

    /* 80-byte header (ignored) */
    unsigned char header[80];
    if (fread(header, 1, 80, fp) != 80) { fclose(fp); return mesh; }

    /* Triangle count */
    unsigned int tri_count = 0;
    if (fread(&tri_count, 4, 1, fp) != 1) { fclose(fp); return mesh; }

    if (tri_count == 0) { fclose(fp); return mesh; }

    int vertex_count = (int)tri_count * 3;

    float *vertices = (float *)malloc(sizeof(float) * (size_t)vertex_count * 3);
    float *normals  = (float *)malloc(sizeof(float) * (size_t)vertex_count * 3);
    int   *indices  = (int   *)malloc(sizeof(int)   * (size_t)tri_count    * 3);

    if (!vertices || !normals || !indices) {
        free(vertices); free(normals); free(indices);
        fclose(fp);
        return mesh;
    }

    for (unsigned int t = 0; t < tri_count; ++t) {
        float normal[3];
        float v0[3], v1[3], v2[3];
        unsigned short attr;

        if (fread(normal, 4, 3, fp) != 3) goto stl_fail;
        if (fread(v0,     4, 3, fp) != 3) goto stl_fail;
        if (fread(v1,     4, 3, fp) != 3) goto stl_fail;
        if (fread(v2,     4, 3, fp) != 3) goto stl_fail;
        if (fread(&attr,  2, 1, fp) != 1) goto stl_fail;

        /* Vertex 0 */
        int base = (int)t * 9;
        vertices[base + 0] = v0[0]; vertices[base + 1] = v0[1]; vertices[base + 2] = v0[2];
        vertices[base + 3] = v1[0]; vertices[base + 4] = v1[1]; vertices[base + 5] = v1[2];
        vertices[base + 6] = v2[0]; vertices[base + 7] = v2[1]; vertices[base + 8] = v2[2];

        /* Store the face normal for each of the three vertices */
        normals[base + 0] = normal[0]; normals[base + 1] = normal[1]; normals[base + 2] = normal[2];
        normals[base + 3] = normal[0]; normals[base + 4] = normal[1]; normals[base + 5] = normal[2];
        normals[base + 6] = normal[0]; normals[base + 7] = normal[1]; normals[base + 8] = normal[2];

        /* Identity indexing */
        indices[(int)t * 3 + 0] = (int)t * 3 + 0;
        indices[(int)t * 3 + 1] = (int)t * 3 + 1;
        indices[(int)t * 3 + 2] = (int)t * 3 + 2;
    }

    fclose(fp);

    mesh.vertices     = vertices;
    mesh.normals      = normals;
    mesh.indices      = indices;
    mesh.vertex_count = vertex_count;
    mesh.tri_count    = (int)tri_count;
    return mesh;

stl_fail:
    free(vertices); free(normals); free(indices);
    fclose(fp);
    return mesh;
}

/* -------------------------------------------------------------------------
 * OBJ loader (two-pass)
 * ---------------------------------------------------------------------- */

static CoreMesh _load_obj(const char *path)
{
    CoreMesh mesh;
    memset(&mesh, 0, sizeof(CoreMesh));

    FILE *fp = fopen(path, "r");
    if (!fp) return mesh;

    /* ------- Pass 1: count lines ------- */
    int v_count  = 0;  /* position lines  */
    int vn_count = 0;  /* normal lines    */
    int f_count  = 0;  /* face lines      */

    char line[512];
    while (fgets(line, sizeof(line), fp)) {
        if (line[0] == 'v' && line[1] == ' ')  ++v_count;
        else if (line[0] == 'v' && line[1] == 'n') ++vn_count;
        else if (line[0] == 'f' && line[1] == ' ')  ++f_count;
    }

    if (v_count == 0 || f_count == 0) { fclose(fp); return mesh; }

    /* ------- Allocate temporaries ------- */
    float *pos_buf = (float *)malloc(sizeof(float) * (size_t)v_count  * 3);
    float *nrm_buf = (float *)malloc(sizeof(float) * (size_t)(vn_count > 0 ? vn_count : 1) * 3);

    /* Unindexed output (face_count * 3 vertices) */
    int    out_vcount = f_count * 3;
    float *out_verts  = (float *)malloc(sizeof(float) * (size_t)out_vcount * 3);
    float *out_norms  = (float *)malloc(sizeof(float) * (size_t)out_vcount * 3);
    int   *out_idx    = (int   *)malloc(sizeof(int)   * (size_t)f_count    * 3);

    if (!pos_buf || !nrm_buf || !out_verts || !out_norms || !out_idx) {
        free(pos_buf); free(nrm_buf);
        free(out_verts); free(out_norms); free(out_idx);
        fclose(fp);
        return mesh;
    }

    /* Zero normals so missing entries remain 0 */
    memset(nrm_buf,  0, sizeof(float) * (size_t)(vn_count > 0 ? vn_count : 1) * 3);
    memset(out_norms, 0, sizeof(float) * (size_t)out_vcount * 3);

    /* ------- Pass 2: fill buffers ------- */
    rewind(fp);

    int vi = 0;   /* position index written so far */
    int ni = 0;   /* normal index written so far   */
    int fi = 0;   /* face index written so far     */

    while (fgets(line, sizeof(line), fp)) {
        if (line[0] == 'v' && line[1] == ' ') {
            /* vertex position */
            float x = 0.0f, y = 0.0f, z = 0.0f;
            sscanf(line + 2, "%f %f %f", &x, &y, &z);
            pos_buf[vi * 3 + 0] = x;
            pos_buf[vi * 3 + 1] = y;
            pos_buf[vi * 3 + 2] = z;
            ++vi;
        } else if (line[0] == 'v' && line[1] == 'n') {
            /* vertex normal */
            float x = 0.0f, y = 0.0f, z = 0.0f;
            sscanf(line + 2, "%f %f %f", &x, &y, &z);
            nrm_buf[ni * 3 + 0] = x;
            nrm_buf[ni * 3 + 1] = y;
            nrm_buf[ni * 3 + 2] = z;
            ++ni;
        } else if (line[0] == 'f' && line[1] == ' ') {
            /* face — supports: v   v/vt   v//vn   v/vt/vn */
            char *tok = line + 2;
            int corner_vi[3] = {0, 0, 0};
            int corner_ni[3] = {0, 0, 0};

            for (int c = 0; c < 3; ++c) {
                /* Skip leading whitespace */
                while (*tok == ' ' || *tok == '\t') ++tok;

                int pv = 0, pt = 0, pn = 0;
                /* Try v/vt/vn or v//vn or v/vt or just v */
                if (sscanf(tok, "%d/%d/%d", &pv, &pt, &pn) == 3) {
                    /* v/vt/vn */
                } else if (sscanf(tok, "%d//%d", &pv, &pn) == 2) {
                    /* v//vn */
                } else if (sscanf(tok, "%d/%d", &pv, &pt) == 2) {
                    /* v/vt — no normal */
                    pn = 0;
                } else {
                    sscanf(tok, "%d", &pv);
                    pn = 0;
                }

                corner_vi[c] = pv - 1;  /* OBJ is 1-based */
                corner_ni[c] = pn - 1;  /* will be -1 if pn==0 */

                /* Advance tok past this token */
                while (*tok && *tok != ' ' && *tok != '\t' && *tok != '\n' && *tok != '\r')
                    ++tok;
            }

            /* Write unindexed output */
            int base_v = fi * 3;
            for (int c = 0; c < 3; ++c) {
                int ov = base_v + c;
                int sv = corner_vi[c];

                /* Bounds-check position index */
                if (sv < 0 || sv >= v_count) {
                    sv = 0;
                }

                out_verts[ov * 3 + 0] = pos_buf[sv * 3 + 0];
                out_verts[ov * 3 + 1] = pos_buf[sv * 3 + 1];
                out_verts[ov * 3 + 2] = pos_buf[sv * 3 + 2];

                int sn = corner_ni[c];
                if (sn >= 0 && sn < vn_count) {
                    out_norms[ov * 3 + 0] = nrm_buf[sn * 3 + 0];
                    out_norms[ov * 3 + 1] = nrm_buf[sn * 3 + 1];
                    out_norms[ov * 3 + 2] = nrm_buf[sn * 3 + 2];
                }
                /* else normals remain 0 from memset */

                out_idx[fi * 3 + c] = ov;
            }
            ++fi;
        }
    }

    fclose(fp);
    free(pos_buf);
    free(nrm_buf);

    mesh.vertices     = out_verts;
    mesh.normals      = out_norms;
    mesh.indices      = out_idx;
    mesh.vertex_count = out_vcount;
    mesh.tri_count    = f_count;
    return mesh;
}

/* -------------------------------------------------------------------------
 * Public API
 * ---------------------------------------------------------------------- */

CoreMesh CoreMesh_Load(const char *path)
{
    CoreMesh zero;
    memset(&zero, 0, sizeof(CoreMesh));

    if (!path) return zero;

    const char *ext = _file_ext(path);
    if (ext && _stricmp_ascii(ext, ".stl") == 0) {
        return _load_stl(path);
    }
    return _load_obj(path);
}

void CoreMesh_Free(CoreMesh *m)
{
    if (!m) return;
    free(m->vertices);
    free(m->normals);
    free(m->indices);
    memset(m, 0, sizeof(CoreMesh));
}

CoreHit CoreMesh_Raycast(const CoreMesh *m, CoreRay ray)
{
    CoreHit result;
    memset(&result, 0, sizeof(CoreHit));
    result.hit      = false;
    result.distance = 1e30f;

    if (!m || m->tri_count == 0) return result;

    const float EPSILON = 1e-7f;

    float best_t = 1e30f;

    for (int t = 0; t < m->tri_count; ++t) {
        int i0 = m->indices[t * 3 + 0];
        int i1 = m->indices[t * 3 + 1];
        int i2 = m->indices[t * 3 + 2];

        float ax = m->vertices[i0 * 3 + 0];
        float ay = m->vertices[i0 * 3 + 1];
        float az = m->vertices[i0 * 3 + 2];

        float bx = m->vertices[i1 * 3 + 0];
        float by = m->vertices[i1 * 3 + 1];
        float bz = m->vertices[i1 * 3 + 2];

        float cx = m->vertices[i2 * 3 + 0];
        float cy = m->vertices[i2 * 3 + 1];
        float cz = m->vertices[i2 * 3 + 2];

        /* Edge vectors */
        float e1x = bx - ax, e1y = by - ay, e1z = bz - az;
        float e2x = cx - ax, e2y = cy - ay, e2z = cz - az;

        /* h = direction x e2 */
        float hx = ray.direction.y * e2z - ray.direction.z * e2y;
        float hy = ray.direction.z * e2x - ray.direction.x * e2z;
        float hz = ray.direction.x * e2y - ray.direction.y * e2x;

        float det = e1x * hx + e1y * hy + e1z * hz;

        if (det > -EPSILON && det < EPSILON) continue;  /* Ray is parallel */

        float inv_det = 1.0f / det;

        /* s = origin - A */
        float sx = ray.origin.x - ax;
        float sy = ray.origin.y - ay;
        float sz = ray.origin.z - az;

        float u = (sx * hx + sy * hy + sz * hz) * inv_det;
        if (u < 0.0f || u > 1.0f) continue;

        /* q = s x e1 */
        float qx = sy * e1z - sz * e1y;
        float qy = sz * e1x - sx * e1z;
        float qz = sx * e1y - sy * e1x;

        float v = (ray.direction.x * qx + ray.direction.y * qy + ray.direction.z * qz) * inv_det;
        if (v < 0.0f || u + v > 1.0f) continue;

        float tval = (e2x * qx + e2y * qy + e2z * qz) * inv_det;
        if (tval < EPSILON) continue;  /* Intersection behind ray origin */

        if (tval < best_t) {
            best_t = tval;

            result.hit      = true;
            result.distance = tval;
            result.point.x  = ray.origin.x + ray.direction.x * tval;
            result.point.y  = ray.origin.y + ray.direction.y * tval;
            result.point.z  = ray.origin.z + ray.direction.z * tval;

            /* Face normal = e1 x e2 (normalized) */
            float nx = e1y * e2z - e1z * e2y;
            float ny = e1z * e2x - e1x * e2z;
            float nz = e1x * e2y - e1y * e2x;
            float len = sqrtf(nx * nx + ny * ny + nz * nz);
            if (len > EPSILON) {
                result.normal.x = nx / len;
                result.normal.y = ny / len;
                result.normal.z = nz / len;
            }
        }
    }

    return result;
}

void CoreMesh_Transform(CoreMesh *m, Matrix transform)
{
    if (!m || m->vertex_count == 0) return;

    for (int i = 0; i < m->vertex_count; ++i) {
        float x = m->vertices[i * 3 + 0];
        float y = m->vertices[i * 3 + 1];
        float z = m->vertices[i * 3 + 2];

        m->vertices[i * 3 + 0] = transform.m0 * x + transform.m4 * y + transform.m8  * z + transform.m12;
        m->vertices[i * 3 + 1] = transform.m1 * x + transform.m5 * y + transform.m9  * z + transform.m13;
        m->vertices[i * 3 + 2] = transform.m2 * x + transform.m6 * y + transform.m10 * z + transform.m14;
    }

    if (!m->normals) return;

    const float EPSILON = 1e-7f;

    for (int i = 0; i < m->vertex_count; ++i) {
        float x = m->normals[i * 3 + 0];
        float y = m->normals[i * 3 + 1];
        float z = m->normals[i * 3 + 2];

        /* Apply rotation/scale only — no translation */
        float nx = transform.m0 * x + transform.m4 * y + transform.m8  * z;
        float ny = transform.m1 * x + transform.m5 * y + transform.m9  * z;
        float nz = transform.m2 * x + transform.m6 * y + transform.m10 * z;

        float len = sqrtf(nx * nx + ny * ny + nz * nz);
        if (len > EPSILON) {
            m->normals[i * 3 + 0] = nx / len;
            m->normals[i * 3 + 1] = ny / len;
            m->normals[i * 3 + 2] = nz / len;
        } else {
            m->normals[i * 3 + 0] = 0.0f;
            m->normals[i * 3 + 1] = 0.0f;
            m->normals[i * 3 + 2] = 0.0f;
        }
    }
}

BoundingBox CoreMesh_BoundingBox(const CoreMesh *m)
{
    BoundingBox box;
    memset(&box, 0, sizeof(BoundingBox));

    if (!m || m->vertex_count == 0) return box;

    box.min.x = box.max.x = m->vertices[0];
    box.min.y = box.max.y = m->vertices[1];
    box.min.z = box.max.z = m->vertices[2];

    for (int i = 1; i < m->vertex_count; ++i) {
        float x = m->vertices[i * 3 + 0];
        float y = m->vertices[i * 3 + 1];
        float z = m->vertices[i * 3 + 2];

        if (x < box.min.x) box.min.x = x;
        if (y < box.min.y) box.min.y = y;
        if (z < box.min.z) box.min.z = z;

        if (x > box.max.x) box.max.x = x;
        if (y > box.max.y) box.max.y = y;
        if (z > box.max.z) box.max.z = z;
    }

    return box;
}
