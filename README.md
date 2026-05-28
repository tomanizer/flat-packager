# Flat Packager

Flat Packager turns a repository into one flat text archive, then rebuilds the
original folder structure from that archive.

The default archive format is newline-delimited JSON with chunked base64 file
payloads. Text files, binary files, empty directories, and symlinks survive the
round trip. Restores validate archive paths, duplicate entries, symlink
ancestry, chunk ordering, and recorded SHA-256 hashes before writing file
contents.

## Install

From a checkout:

```bash
python3 -m pip install .
```

For development:

```bash
python3 -m pip install -e ".[dev]"
```

This installs four console commands:

```bash
flat-pack --help
flat-unpack --help
flat-inspect --help
flat-verify --help
```

The repository also keeps compatibility wrappers:

```bash
python3 pack_repo.py --help
python3 unpack_repo.py --help
```

## Pack a Repository

Pack a local repository:

```bash
flat-pack /path/to/repo repo.flat.txt
```

Pack a public GitHub repository by shorthand or Git URL:

```bash
flat-pack owner/repo repo.flat.txt
flat-pack https://github.com/owner/repo.git repo.flat.txt
```

Remote repositories are cloned with `--depth 1` by default. You can select a
branch or tag, include submodules, or request a full clone:

```bash
flat-pack owner/repo repo.flat.txt --branch main
flat-pack owner/repo repo.flat.txt --recurse-submodules
flat-pack owner/repo repo.flat.txt --no-shallow
```

By default, local directory scans include all files and directories except
`.git`. To include only tracked git files:

```bash
flat-pack /path/to/repo repo.flat.txt --tracked-only
```

Useful options:

```bash
flat-pack /path/to/repo repo.flat.txt --exclude "node_modules/*"
flat-pack /path/to/repo repo.flat.txt --max-file-bytes 10485760
flat-pack /path/to/repo repo.flat.txt --include-git
```

## Format and Compression

New archives use format version 2 by default. Version 2 stores each regular
file as metadata plus one or more base64 chunk records, which keeps memory usage
bounded while packing and restoring large files.

```bash
flat-pack /path/to/repo repo.flat.txt --chunk-size 1048576
```

To write the legacy inline format:

```bash
flat-pack /path/to/repo repo.v1.flat.txt --format-version 1
```

To gzip-compress an archive:

```bash
flat-pack /path/to/repo repo.flat.txt.gz --compress gzip
```

`--compress auto` is the default and writes gzip when the output path ends in
`.gz`. Restore, inspect, and verify commands auto-detect gzip archives.

## Restore a Repository

```bash
flat-unpack repo.flat.txt restored-repo
```

If the output directory already contains files:

```bash
flat-unpack repo.flat.txt restored-repo --overwrite
```

Restores are staged in a temporary sibling directory and moved into place only
after the full archive validates and writes successfully. If validation fails,
the existing output directory is left unchanged.

Validate an archive without writing files:

```bash
flat-unpack repo.flat.txt restored-repo --verify-only
```

You can also validate without naming an output directory:

```bash
flat-verify repo.flat.txt
```

On systems where symlink creation is unavailable:

```bash
flat-unpack repo.flat.txt restored-repo --no-symlinks
```

## Inspect an Archive

```bash
flat-inspect repo.flat.txt
flat-inspect repo.flat.txt --json
flat-inspect repo.flat.txt --limit 20
```

Inspection validates the archive first, then reports version, compression,
source metadata, entry counts, total file bytes, chunk count, largest files, and
symlinks.

## Safety Model

Flat Packager is intended for moving repository snapshots, not for executing or
trusting their contents. Treat archives from untrusted sources the same way you
would treat a zip file from an untrusted source.

The restore step rejects:

- absolute archive paths
- `..`, `.`, empty, or backslash path segments
- duplicate archive paths
- file or directory records nested underneath an archived symlink
- chunk records that are missing, out of order, corrupt, or attached to the
  wrong file
- corrupt base64, size, or SHA-256 data

Restored symlinks may still point outside the restored repository, because that
is valid repository content. Use `--no-symlinks` if you want symlink targets
written as text files instead.

## Limitations

- File contents are base64 encoded, so archives are larger than raw file bytes.
  Use gzip compression when archive size matters.
- Legacy format version 1 stores each file as one JSON record and is
  memory-heavy for large files. Version 2 is chunked and is the default.
- Remote GitHub packing depends on local `git` and whatever authentication your
  git installation already has configured.
- Git LFS files are captured as whatever exists in the cloned checkout. If LFS
  smudge is disabled or unavailable, pointer files may be archived instead of
  large objects.
- This stores file bytes, directory modes, file modes, and symlink targets. It
  does not preserve owners, groups, extended attributes, ACLs, hard links,
  timestamps, git history, or ignored remote refs.

## Test

```bash
python3 -m unittest discover -s tests
```

Build a wheel:

```bash
python3 -m pip install ".[dev]"
python3 -m build
```
