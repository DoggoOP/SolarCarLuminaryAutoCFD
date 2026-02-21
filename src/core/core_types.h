#ifndef CORE_TYPES_H
#define CORE_TYPES_H

#include <stdbool.h>

/*
 * Minimal math types for the headless CLI core.
 * Layout-compatible with raylib/raymath equivalents.
 * RL_* guards allow safe co-inclusion with raymath.h in .c files:
 * include app_core.h first (defines RL_* guards), then raymath.h
 * (skips typedefs, still provides function bodies).
 */

#if !defined(RL_VECTOR3_TYPE)
typedef struct Vector3 { float x; float y; float z; } Vector3;
#define RL_VECTOR3_TYPE
#endif

#if !defined(RL_MATRIX_TYPE)
typedef struct Matrix {
    float m0, m4, m8,  m12;
    float m1, m5, m9,  m13;
    float m2, m6, m10, m14;
    float m3, m7, m11, m15;
} Matrix;
#define RL_MATRIX_TYPE
#endif

#if !defined(RL_BOUNDING_BOX_TYPE)
typedef struct BoundingBox { Vector3 min; Vector3 max; } BoundingBox;
#define RL_BOUNDING_BOX_TYPE
#endif

#ifndef DEG2RAD
#define DEG2RAD (3.14159265358979323846f / 180.0f)
#endif

/*
 * CoreMesh — replaces Raylib's Model + Mesh.
 * Vertices and normals are baked into world space (transform applied on load).
 */
typedef struct {
    float *vertices;   /* xyz triplets; count = vertex_count * 3 */
    float *normals;    /* xyz triplets; count = vertex_count * 3 */
    int   *indices;    /* triangle index triplets; count = tri_count * 3 */
    int    vertex_count;
    int    tri_count;
} CoreMesh;

/* Replaces Raylib's Ray */
typedef struct {
    Vector3 origin;
    Vector3 direction;
} CoreRay;

/* Replaces Raylib's RayCollision */
typedef struct {
    bool    hit;
    float   distance;
    Vector3 point;
    Vector3 normal;
} CoreHit;

#endif /* CORE_TYPES_H */
