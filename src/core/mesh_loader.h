#ifndef MESH_LOADER_H
#define MESH_LOADER_H

#include "core_types.h"

/* Load OBJ or STL file into a CoreMesh.
 * Returns a zeroed struct (tri_count == 0) on failure. */
CoreMesh CoreMesh_Load(const char *path);

/* Free heap memory inside mesh and zero the struct. */
void CoreMesh_Free(CoreMesh *m);

/* Möller–Trumbore ray-triangle intersection.
 * Returns the closest hit; hit.hit == false if no intersection. */
CoreHit CoreMesh_Raycast(const CoreMesh *m, CoreRay ray);

/* Apply a transform matrix to all vertices and normals in-place.
 * Use after loading to bake scale/rotation into world space. */
void CoreMesh_Transform(CoreMesh *m, Matrix transform);

/* Compute axis-aligned bounding box from vertices. */
BoundingBox CoreMesh_BoundingBox(const CoreMesh *m);

#endif /* MESH_LOADER_H */
