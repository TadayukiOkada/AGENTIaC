#!/usr/bin/env bash
set -euo pipefail
root=${1:?project root}; rev=${2:?commit}; arch=${3:?CUDA arch}; jobs=${4:-4}
[[ $rev =~ ^[a-f0-9]{40}$ && $arch =~ ^[0-9]+$ && $jobs =~ ^[0-9]+$ ]]
mkdir -p "$root/backends" "$root/logs"
exec 9>"$root/backends/build.lock"
flock -n 9 || { echo 'build already running'; exit 75; }
dest="$root/backends/llama-$rev"
if [[ ! -d $dest/.git ]]; then
  git init "$dest"
  git -C "$dest" remote add origin https://github.com/ggml-org/llama.cpp.git
  git -C "$dest" fetch --depth 1 origin "$rev"
  git -C "$dest" checkout --detach FETCH_HEAD
fi
[[ $(git -C "$dest" rev-parse HEAD) == "$rev" ]]
[[ -z $(git -C "$dest" status --porcelain --untracked-files=no) ]]
cmake -S "$dest" -B "$dest/build-sm$arch" \
 -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="$arch" \
 -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc -DGGML_NATIVE=OFF \
 -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=ON -DLLAMA_BUILD_SERVER=ON \
 -DLLAMA_OPENSSL=OFF -DCMAKE_INSTALL_PREFIX="$dest/install-sm$arch"
cmake --build "$dest/build-sm$arch" --target llama-server llama-bench --parallel "$jobs"
# Keep all runtime shared objects together; do not install over host libraries.
mkdir -p "$dest/runtime-sm$arch"
cp -a "$dest/build-sm$arch/bin/." "$dest/runtime-sm$arch/"
{
 git -C "$dest" rev-parse HEAD
 /usr/local/cuda/bin/nvcc --version
 g++ --version
 cmake --version
 sha256sum "$dest/runtime-sm$arch/llama-server" "$dest/runtime-sm$arch/llama-bench"
} > "$dest/runtime-sm$arch/build-metadata.txt"
