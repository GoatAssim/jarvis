"""Host allow-lists for the Minecraft mod sources.  Constants only: never read from config or env.

A configurable host would let a hand-edit (or a redirect) send the CurseForge key somewhere else.
Only hosts the owner's probe (25 Oct 2026) actually reached are listed.  net.py reads these blocks
and nothing else does.

One block per owner, with 12 or more blank/comment lines between blocks so that separate patches
never touch the same lines.  Do not move a block closer to another one.
"""

# ------------------------------------------------------------------ block 1 of 4: Modrinth
MODRINTH_HOSTS = (
    "api.modrinth.com",
    "cdn.modrinth.com",
)

#
#
#
#
#
#
#
#
#
#
#
#
#
# ------------------------------------------------------------------ block 2 of 4: CurseForge
CURSEFORGE_HOSTS = (
    "api.curseforge.com",
    "edge.forgecdn.net",
)

#
#
#
#
#
#
#
#
#
#
#
#
#
# ------------------------------------------------------------------ block 3 of 4: OptiFine
OPTIFINE_HOSTS = (
    "optifine.net",
)

#
#
#
#
#
#
#
#
#
#
#
#
#
# ------------------------------------------------------------------ block 4 of 4: loader metadata
LOADER_META_HOSTS = (
    "meta.fabricmc.net",
    "meta.quiltmc.org",
    "files.minecraftforge.net",
    "maven.minecraftforge.net",
    "maven.neoforged.net",
)
