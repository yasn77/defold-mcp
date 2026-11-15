# Shaders and materials

A `.material` names a vertex program (`.vp`), a fragment program (`.fp`), the
constants they read, the samplers they bind and the tags the render script uses
to draw them. Get the version header and the uniform blocks right and everything
else follows.

## Pipeline selection

The first line decides everything:

```glsl
#version 140
```

No version header means the **legacy** ES2 pipeline; `#version 140` or higher
means the **modern** pipeline, which is cross-compiled to GLSL ES for mobile and
WebGL. Write modern shaders.

| Legacy (ES2) | Modern (`#version 140`) |
|---|---|
| `attribute vec4 position;` | `in vec4 position;` |
| `varying vec2 var_texcoord0;` (vertex) | `out vec2 var_texcoord0;` |
| `varying vec2 var_texcoord0;` (fragment) | `in vec2 var_texcoord0;` |
| `gl_FragColor = c;` | `out vec4 out_fragColor;` then `out_fragColor = c;` |
| `texture2D(s, uv)` | `texture(s, uv)` |
| loose `uniform vec4 tint;` | member of a named `uniform` block |

Opaque uniforms — samplers, images, atomics, storage buffers — stay outside the
block. Every other uniform must sit inside one.

## A complete sprite pair

Vertex program, `/main/sprite.vp`:

```glsl
#version 140

uniform vs_uniforms
{
    mat4 view_proj;
};

in vec4 position;   // already in world space for sprites
in vec2 texcoord0;

out vec2 var_texcoord0;

void main()
{
    gl_Position = view_proj * vec4(position.xyz, 1.0);
    var_texcoord0 = texcoord0;
}
```

Fragment program, `/main/sprite.fp`:

```glsl
#version 140

in vec2 var_texcoord0;

out vec4 out_fragColor;

uniform sampler2D texture_sampler;

uniform fs_uniforms
{
    vec4 tint;
};

void main()
{
    // Runtime textures are premultiplied, so premultiply the tint too.
    vec4 tint_pm = vec4(tint.xyz * tint.w, tint.w);
    out_fragColor = texture(texture_sampler, var_texcoord0) * tint_pm;
}
```

The block names are free-form (`vs_uniforms`, `fs_uniforms`, `vx_uniforms`,
`general_vp`, …); every member is exposed to the material editor and to
`go.set`/`go.get` as an individual constant, so the block changes grouping, not
the API.

## The sprite material contract

`/builtins/materials/sprite.material` binds exactly these names. Match them and
your shader drops into existing sprites unchanged:

* sampler `texture_sampler` — the component's *Image* (atlas or texture)
* constant `tint` (`vec4`) — user constant, default white
* vertex constant `view_proj` — combined view and projection matrices
* tags — the default render script draws the `sprite` tag

Other components bind other names: GUI nodes use `texture_sampler` plus a `tint`
user constant and a `color` vertex attribute; models use the glTF samplers below.

## Materials

A `.material` carries:

* *Vertex program* / *Fragment program* — `/main/sprite.vp`, `/main/sprite.fp`.
* *Vertex constants* and *Fragment constants* — one entry per uniform name, with
  type (`CONSTANT_TYPE_VIEWPROJ`, `CONSTANT_TYPE_WORLD`, `CONSTANT_TYPE_NORMAL`,
  `CONSTANT_TYPE_TIME`, `CONSTANT_TYPE_USER`, …).
* *Samplers* — filter and wrap per sampler name.
* *Tags* — a bitmask consulted by `render.predicate()`; at most 32 in a project.
* *Vertex space* — `local` or `world`. Local vertex space is required for
  instancing and is what the built-in glTF materials use.

Engine-supplied constants worth knowing: `CONSTANT_TYPE_WORLD` (model to world),
`CONSTANT_TYPE_VIEW`, `CONSTANT_TYPE_PROJECTION`, their `*_INVERSE` variants,
`CONSTANT_TYPE_WORLDVIEWPROJ`, `CONSTANT_TYPE_NORMAL`, and `CONSTANT_TYPE_TIME`
(a `vec4`: elapsed seconds in `.x`, frame delta in `.y`).

## Instancing

Instancing is for **model** components only and switches on automatically when
the material uses local vertex space and declares at least one per-instance
attribute:

```glsl
#version 140

in vec4 position;
in vec2 texcoord0;
in vec3 normal;

// Per-instance attributes: step function "Instance" in the material.
in mat4 mtx_world;
in mat4 mtx_normal;

uniform vs_uniforms
{
    mat4 view_proj;
};

out vec2 var_texcoord0;
out vec3 var_normal;

void main()
{
    vec4 world = mtx_world * vec4(position.xyz, 1.0);
    gl_Position = view_proj * world;
    var_texcoord0 = texcoord0;
    var_normal = mat3(mtx_normal) * normal;
}
```

`mtx_world` and `mtx_normal` receive their semantic types
(`SEMANTIC_TYPE_WORLD_MATRIX`, `SEMANTIC_TYPE_NORMAL_MATRIX`) from their names,
so declaring them is enough. Constants must be identical across the batch, or
move them into an attribute or a texture.

## Normals: view space versus world space

`CONSTANT_TYPE_NORMAL` — and therefore `mtx_normal` — is
`transpose(inverse(view * world))`: a **view-space** normal matrix. Anything that
compares against a world-space vector (a cubemap reflection, a sun direction)
needs a world-space matrix derived from `mtx_world`:

```glsl
// transpose(inverse(mat3(mtx_world))) without the inverse, for uniform scale.
mat3 adjoint(mat4 m)
{
    return mat3(
        cross(m[1].xyz, m[2].xyz),
        cross(m[2].xyz, m[0].xyz),
        cross(m[0].xyz, m[1].xyz)
    );
}
```

If you need the camera position in world space, invert the view matrix's
rotation: `-(transpose(mat3(view)) * view[3].xyz)`.

## glTF PBR hooks

The four built-in model materials — `gltf.material`, `gltf_instanced.material`,
`gltf_skinned.material`, `gltf_skinned_instanced.material` — share `gltf.fp`. To
customise, copy the material and the fragment shader into the project and keep
the vertex program, sampler names and local vertex space intact.

Sampler names for manual bindings:

| Texture | Sampler |
|---|---|
| Base colour | `PbrMetallicRoughness_baseColorTexture` |
| Metallic / roughness | `PbrMetallicRoughness_metallicRoughnessTexture` |
| Normal | `PbrMaterial_normalTexture` |
| Occlusion | `PbrMaterial_occlusionTexture` |
| Emissive | `PbrMaterial_emissiveTexture` |

The helper include `/builtins/materials/gltf_sampling.glsl` resolves the raw
`PbrMaterial` uniform block into a linear `PBRMaterial` value;
`/builtins/materials/gltf_lighting.glsl` provides direct and ambient lighting:

```glsl
#version 140

#include "/builtins/materials/gltf_sampling.glsl"
#include "/builtins/materials/gltf_lighting.glsl"

out vec4 out_fragColor;

void main()
{
    PBRMaterial inputs = pbr_sample_material(var_texcoord0, var_color);
    inputs.roughness = max(inputs.roughness, 0.35);   // edit before BRDF setup

    PBRSurface surface = pbr_sample_surface(inputs, var_texcoord0);
    if ((!inputs.doubleSided && !gl_FrontFacing) || pbr_should_discard_alpha(inputs))
    {
        discard;
    }

    MaterialInfo material = pbr_create_material_info(inputs);
    PBRLighting lighting = pbr_create_lighting();
    if (!inputs.unlit)
    {
        lighting.direct = pbr_evaluate_direct_lighting(material, surface, var_view);
        lighting.indirect = pbr_evaluate_ambient_lighting(material, ambient_light());
    }

    vec3 color = pbr_compose_lighting(inputs, lighting, inputs.occlusion, inputs.occlusion);
    out_fragColor = vec4(pbr_linear_to_srgb(color), pbr_get_alpha(inputs));
}
```

Sampled inputs must be edited *before* `pbr_create_material_info()`. If you shift
the surface position or normals, call `pbr_finalize_surface(surface, var_view)`.
Base colour and emissive texels are sRGB-decoded on read and re-encoded on write;
metallic, roughness, normal and occlusion maps are linear.

## Snippets and editor-only code

`#include "/main/lib/lighting.glsl"` pulls in project `.glsl` files (project
relative, or relative to the including file). Guard shared snippets, since two
includes of the same file are a duplicate-symbol error, and use `#ifdef EDITOR`
for a visible fallback when a material cannot render in the Editor viewport:

```glsl
#ifndef MY_LIGHTING_GLSL
#define MY_LIGHTING_GLSL
// ...
#endif
```
