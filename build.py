#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (c) 2019 The ungoogled-chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""
ungoogled-chromium build script for Microsoft Windows
"""

import sys
import time
import argparse
import os
import re
import shutil
import subprocess
import ctypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'ungoogled-chromium' / 'utils'))
import downloads
import domain_substitution
import prune_binaries
import patches
from _common import ENCODING, USE_REGISTRY, ExtractorEnum, get_logger
sys.path.pop(0)

_ROOT_DIR = Path(__file__).resolve().parent
_PATCH_BIN_RELPATH = Path('third_party/git/usr/bin/patch.exe')
_PATCH_FAILED_FILES_DIR = _ROOT_DIR / 'build' / 'patch_failed_files'


def _extract_files_from_patch(patch_path):
    """
    Extracts the list of files that a patch modifies.
    Returns a list of relative file paths from the patch.
    """
    files = []
    try:
        with open(patch_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                # Match lines like "--- a/path/to/file" or "+++ b/path/to/file"
                if line.startswith('--- a/') or line.startswith('+++ b/'):
                    # Extract path after "--- a/" or "+++ b/"
                    path = line[6:].strip()
                    # Remove any trailing tab and timestamp (e.g., "\t2021-01-01 00:00:00")
                    if '\t' in path:
                        path = path.split('\t')[0]
                    if path and path not in files and path != '/dev/null':
                        files.append(path)
                # Also match "--- path/to/file" or "+++ path/to/file" (without a/ or b/ prefix)
                elif line.startswith('--- ') or line.startswith('+++ '):
                    # Extract path after "--- " or "+++ "
                    path = line[4:].strip()
                    # Remove any trailing tab and timestamp
                    if '\t' in path:
                        path = path.split('\t')[0]
                    # Skip /dev/null and already extracted files
                    if path and path not in files and path != '/dev/null':
                        files.append(path)
                # Also match "diff --git a/path b/path" format
                elif line.startswith('diff --git '):
                    parts = line.strip().split(' ')
                    if len(parts) >= 4:
                        # Extract path from "a/path"
                        path = parts[2]
                        if path.startswith('a/'):
                            path = path[2:]
                            if path and path not in files:
                                files.append(path)
    except (OSError, IOError, UnicodeDecodeError) as e:
        get_logger().warning('Failed to parse patch file %s: %s', patch_path, e)
    return files


def _copy_failed_files(source_tree, patch_path, failed_files_dir):
    """
    Copies the files that would be affected by a patch to the failed files directory.
    This captures the state of the files after successful patches but before the failed patch.
    """
    files = _extract_files_from_patch(patch_path)
    if not files:
        get_logger().warning('No files extracted from patch: %s', patch_path)
        return []
    
    copied_files = []
    failed_files_dir.mkdir(parents=True, exist_ok=True)
    
    for file_path in files:
        source_file = source_tree / file_path
        if source_file.exists():
            # Create subdirectory structure in failed_files_dir
            dest_file = failed_files_dir / file_path
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(source_file, dest_file)
                copied_files.append(str(dest_file))
                get_logger().info('Copied failed file: %s', file_path)
            except (OSError, IOError, shutil.Error) as e:
                get_logger().warning('Failed to copy file %s: %s', file_path, e)
        else:
            get_logger().info('Source file does not exist (new file in patch): %s', file_path)
    
    # Also save the patch file itself for reference (use patch stem to avoid conflicts)
    patch_dest = failed_files_dir / f'failed_patch_{patch_path.stem}.patch'
    try:
        shutil.copy2(patch_path, patch_dest)
        copied_files.append(str(patch_dest))
        get_logger().info('Copied failed patch file: %s', patch_path.name)
    except (OSError, IOError, shutil.Error) as e:
        get_logger().warning('Failed to copy patch file: %s', e)
    
    return copied_files


def apply_patches_with_capture(patch_path_iter, tree_path, patch_bin_path=None, failed_files_dir=None):
    """
    Applies patches and captures affected files when a patch fails.
    
    This is similar to patches.apply_patches() but captures the state of files
    when a patch fails for debugging purposes.
    """
    patch_paths = list(patch_path_iter)
    patch_bin_path = patches.find_and_check_patch(patch_bin_path=patch_bin_path)
    
    if failed_files_dir is None:
        failed_files_dir = _PATCH_FAILED_FILES_DIR
    
    logger = get_logger()
    for patch_path, patch_num in zip(patch_paths, range(1, len(patch_paths) + 1)):
        cmd = [
            str(patch_bin_path), '-p1', '--ignore-whitespace', '-i',
            str(patch_path), '-d',
            str(tree_path), '--no-backup-if-mismatch', '--forward'
        ]
        logger.info('* Applying %s (%s/%s)', patch_path.name, patch_num, len(patch_paths))
        logger.debug(' '.join(cmd))
        
        result = subprocess.run(cmd, check=False)
        
        if result.returncode != 0:
            logger.error('Patch failed: %s', patch_path.name)
            logger.info('Capturing affected files for debugging...')
            
            # Copy the affected files to the failed files directory
            _copy_failed_files(tree_path, patch_path, failed_files_dir)
            
            # Re-raise the error to maintain the original behavior
            raise subprocess.CalledProcessError(result.returncode, cmd)


def _get_vcvars_path(name='64'):
    """
    Returns the path to the corresponding vcvars*.bat path

    As of VS 2017, name can be one of: 32, 64, all, amd64_x86, x86_amd64
    """
    vswhere_exe = '%ProgramFiles(x86)%\\Microsoft Visual Studio\\Installer\\vswhere.exe'
    result = subprocess.run(
        '"{}" -prerelease -latest -property installationPath'.format(vswhere_exe),
        shell=True,
        check=True,
        stdout=subprocess.PIPE,
        universal_newlines=True)
    vcvars_path = Path(result.stdout.strip(), 'VC/Auxiliary/Build/vcvars{}.bat'.format(name))
    if not vcvars_path.exists():
        raise RuntimeError(
            'Could not find vcvars batch script in expected location: {}'.format(vcvars_path))
    return vcvars_path


def _run_build_process(*args, **kwargs):
    """
    Runs the subprocess with the correct environment variables for building
    """
    # Add call to set VC variables
    cmd_input = ['call "%s" >nul' % _get_vcvars_path()]
    cmd_input.append('set DEPOT_TOOLS_WIN_TOOLCHAIN=0')
    cmd_input.append(' '.join(map('"{}"'.format, args)))
    cmd_input.append('exit\n')
    subprocess.run(('cmd.exe', '/k'),
                   input='\n'.join(cmd_input),
                   check=True,
                   encoding=ENCODING,
                   **kwargs)


def _run_build_process_timeout(*args, timeout):
    """
    Runs the subprocess with the correct environment variables for building
    """
    # Add call to set VC variables
    cmd_input = ['call "%s" >nul' % _get_vcvars_path()]
    cmd_input.append('set DEPOT_TOOLS_WIN_TOOLCHAIN=0')
    cmd_input.append(' '.join(map('"{}"'.format, args)))
    cmd_input.append('exit\n')
    with subprocess.Popen(('cmd.exe', '/k'), encoding=ENCODING, stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP) as proc:
        proc.stdin.write('\n'.join(cmd_input))
        proc.stdin.close()
        try:
            proc.wait(timeout)
            if proc.returncode != 0:
                raise RuntimeError('Build failed!')
        except subprocess.TimeoutExpired:
            print('Sending keyboard interrupt')
            for _ in range(3):
                ctypes.windll.kernel32.GenerateConsoleCtrlEvent(1, proc.pid)
                time.sleep(1)
            try:
                proc.wait(10)
            except:
                proc.kill()
            raise KeyboardInterrupt


def _make_tmp_paths():
    """Creates TMP and TEMP variable dirs so ninja won't fail"""
    tmp_path = Path(os.environ['TMP'])
    if not tmp_path.exists():
        tmp_path.mkdir()
    tmp_path = Path(os.environ['TEMP'])
    if not tmp_path.exists():
        tmp_path.mkdir()


def main():
    """CLI Entrypoint"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--disable-ssl-verification',
        action='store_true',
        help='Disables SSL verification for downloading')
    parser.add_argument(
        '--7z-path',
        dest='sevenz_path',
        default=USE_REGISTRY,
        help=('Command or path to 7-Zip\'s "7z" binary. If "_use_registry" is '
              'specified, determine the path from the registry. Default: %(default)s'))
    parser.add_argument(
        '--winrar-path',
        dest='winrar_path',
        default=USE_REGISTRY,
        help=('Command or path to WinRAR\'s "winrar.exe" binary. If "_use_registry" is '
              'specified, determine the path from the registry. Default: %(default)s'))
    parser.add_argument(
        '-j',
        type=int,
        dest='thread_count',
        help=('Number of CPU threads to use for compiling'))
    parser.add_argument(
        '--ci',
        action='store_true'
    )
    parser.add_argument(
        '--x86',
        action='store_true'
    )
    parser.add_argument(
        '--arm',
        action='store_true'
    )
    parser.add_argument(
        '--tarball',
        action='store_true'
    )
    args = parser.parse_args()

    # Set common variables
    source_tree = _ROOT_DIR / 'build' / 'src'
    downloads_cache = _ROOT_DIR / 'build' / 'download_cache'

    if not args.ci or not (source_tree / 'BUILD.gn').exists():
        # Setup environment
        source_tree.mkdir(parents=True, exist_ok=True)
        downloads_cache.mkdir(parents=True, exist_ok=True)
        _make_tmp_paths()

        # Extractors
        extractors = {
            ExtractorEnum.SEVENZIP: args.sevenz_path,
            ExtractorEnum.WINRAR: args.winrar_path,
        }

        # Prepare source folder
        if args.tarball:
            # Download chromium tarball
            get_logger().info('Downloading chromium tarball...')
            download_info = downloads.DownloadInfo([_ROOT_DIR / 'ungoogled-chromium' / 'downloads.ini'])
            downloads.retrieve_downloads(download_info, downloads_cache, None, True, args.disable_ssl_verification)
            try:
                downloads.check_downloads(download_info, downloads_cache, None)
            except downloads.HashMismatchError as exc:
                get_logger().error('File checksum does not match: %s', exc)
                exit(1)

            # Unpack chromium tarball
            get_logger().info('Unpacking chromium tarball...')
            downloads.unpack_downloads(download_info, downloads_cache, None, source_tree, extractors)
        else:
            # Clone sources
            subprocess.run([sys.executable, str(Path('ungoogled-chromium', 'utils', 'clone.py')), '-o', 'build\\src', '-p', 'win32' if args.x86 else 'win-arm64' if args.arm else 'win64'], check=True)

        # Retrieve windows downloads
        get_logger().info('Downloading required files...')
        download_info_win = downloads.DownloadInfo([_ROOT_DIR / 'downloads.ini'])
        downloads.retrieve_downloads(download_info_win, downloads_cache, None, True, args.disable_ssl_verification)
        try:
            downloads.check_downloads(download_info_win, downloads_cache, None)
        except downloads.HashMismatchError as exc:
            get_logger().error('File checksum does not match: %s', exc)
            exit(1)

        # Prune binaries
        pruning_list = (_ROOT_DIR / 'ungoogled-chromium' / 'pruning.list') if args.tarball else (_ROOT_DIR  / 'pruning.list')
        unremovable_files = prune_binaries.prune_files(
            source_tree,
            pruning_list.read_text(encoding=ENCODING).splitlines()
        )
        if unremovable_files:
            get_logger().error('Files could not be pruned: %s', unremovable_files)
            parser.exit(1)

        # Unpack downloads
        DIRECTX = source_tree / 'third_party' / 'microsoft_dxheaders' / 'src'
        ESBUILD = source_tree / 'third_party' / 'devtools-frontend' / 'src' / 'third_party' / 'esbuild'
        if DIRECTX.exists():
            shutil.rmtree(DIRECTX)
            DIRECTX.mkdir()
        if ESBUILD.exists():
            shutil.rmtree(ESBUILD)
            ESBUILD.mkdir()
        get_logger().info('Unpacking downloads...')
        downloads.unpack_downloads(download_info_win, downloads_cache, None, source_tree, extractors)

        # Apply patches
        # First, ungoogled-chromium-patches
        apply_patches_with_capture(
            patches.generate_patches_from_series(_ROOT_DIR / 'ungoogled-chromium' / 'patches', resolve=True),
            source_tree,
            patch_bin_path=(source_tree / _PATCH_BIN_RELPATH)
        )
        # Then Windows-specific patches
        apply_patches_with_capture(
            patches.generate_patches_from_series(_ROOT_DIR / 'patches', resolve=True),
            source_tree,
            patch_bin_path=(source_tree / _PATCH_BIN_RELPATH)
        )

        # Substitute domains
        domain_substitution_list = (_ROOT_DIR / 'ungoogled-chromium' / 'domain_substitution.list') if args.tarball else (_ROOT_DIR  / 'domain_substitution.list')
        domain_substitution.apply_substitution(
            _ROOT_DIR / 'ungoogled-chromium' / 'domain_regex.list',
            domain_substitution_list,
            source_tree,
            None
        )

    # Check if rust-toolchain folder has been populated
    HOST_CPU_IS_64BIT = sys.maxsize > 2**32
    RUST_DIR_DST = source_tree / 'third_party' / 'rust-toolchain'
    RUST_DIR_SRC64 = source_tree / 'third_party' / 'rust-toolchain-x64'
    RUST_DIR_SRC86 = source_tree / 'third_party' / 'rust-toolchain-x86'
    RUST_DIR_SRCARM = source_tree / 'third_party' / 'rust-toolchain-arm'
    RUST_FLAG_FILE = RUST_DIR_DST / 'INSTALLED_VERSION'
    if not args.ci or not RUST_FLAG_FILE.exists():
        # Directories to copy from source to target folder
        DIRS_TO_COPY = ['bin', 'lib']

        # Loop over all source folders
        for rust_dir_src in [RUST_DIR_SRC64, RUST_DIR_SRC86, RUST_DIR_SRCARM]:
            # Loop over all dirs to copy
            for dir_to_copy in DIRS_TO_COPY:
                # Copy bin folder for host architecture
                if (dir_to_copy == 'bin') and (HOST_CPU_IS_64BIT != (rust_dir_src == RUST_DIR_SRC64)):
                    continue

                # Create target dir
                target_dir = RUST_DIR_DST / dir_to_copy
                if not os.path.isdir(target_dir):
                    os.makedirs(target_dir)

                # Loop over all subfolders of the rust source dir
                for cp_src in rust_dir_src.glob(f'*/{dir_to_copy}/*'):
                    cp_dst = target_dir / cp_src.name
                    if cp_src.is_dir():
                        shutil.copytree(cp_src, cp_dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(cp_src, cp_dst)

        # Generate version file
        with open(RUST_FLAG_FILE, 'w') as f:
            subprocess.run([source_tree / 'third_party' / 'rust-toolchain-x64' / 'rustc' / 'bin' / 'rustc.exe', '--version'], stdout=f)

    if not args.ci or not (source_tree / 'out/Default').exists():
        # Output args.gn
        (source_tree / 'out/Default').mkdir(parents=True)
        gn_flags = (_ROOT_DIR / 'ungoogled-chromium' / 'flags.gn').read_text(encoding=ENCODING)
        gn_flags += '\n'
        windows_flags = (_ROOT_DIR / 'flags.windows.gn').read_text(encoding=ENCODING)
        if args.x86:
            windows_flags = windows_flags.replace('x64', 'x86')
        elif args.arm:
            windows_flags = windows_flags.replace('x64', 'arm64')
        if args.tarball:
            windows_flags += '\nchrome_pgo_phase=0\n'
        gn_flags += windows_flags
        (source_tree / 'out/Default/args.gn').write_text(gn_flags, encoding=ENCODING)

    # Enter source tree to run build commands
    os.chdir(source_tree)

    if not args.ci or not os.path.exists('out\\Default\\gn.exe'):
        # Run GN bootstrap
        _run_build_process(
            sys.executable, 'tools\\gn\\bootstrap\\bootstrap.py', '-o', 'out\\Default\\gn.exe',
            '--skip-generate-buildfiles')

        # Run gn gen
        _run_build_process('out\\Default\\gn.exe', 'gen', 'out\\Default', '--fail-on-unused-args')

    if not args.ci or not os.path.exists('third_party\\rust-toolchain\\bin\\bindgen.exe'):
        # Build bindgen
        _run_build_process(
            sys.executable,
            'tools\\rust\\build_bindgen.py', '--skip-test')

    # Ninja commandline
    ninja_commandline = ['third_party\\ninja\\ninja.exe']
    if args.thread_count is not None:
        ninja_commandline.append('-j')
        ninja_commandline.append(args.thread_count)
    ninja_commandline.append('-C')
    ninja_commandline.append('out\\Default')
    ninja_commandline.append('chrome')
    ninja_commandline.append('chromedriver')
    ninja_commandline.append('mini_installer')

    # Run ninja
    if args.ci:
        _run_build_process_timeout(*ninja_commandline, timeout=4.7*60*60)
        # package
        os.chdir(_ROOT_DIR)
        subprocess.run([sys.executable, 'package.py'])
    else:
        _run_build_process(*ninja_commandline)


if __name__ == '__main__':
    main()
