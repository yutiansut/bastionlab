import os
from setuptools import find_packages, setup
from setuptools.command.build_py import build_py
import pkg_resources
import re
from pybind11.setup_helpers import Pybind11Extension, build_ext


def read(path):
    return open(os.path.join(os.path.dirname(__file__), path)).read()


DIR = os.path.dirname(__file__) or os.getcwd()
PROTO_FILES = ["bastionlab.proto","attestation.proto"]
PROTO_PATH = os.path.join(os.path.dirname(DIR), "protos")
LONG_DESCRIPTION = read("README.md")
PKG_NAME = "bastionlab"

ext_modules = [
    Pybind11Extension("_attestation_c",
        ["attestation_C/lib.cpp"],
        libraries = ['ssl', 'crypto'],
        cxx_std=11)
]

def find_version():
    version_file = read(f"src/{PKG_NAME}/version.py")
    version_re = r"__version__ = \"(?P<version>.+)\""
    version = re.match(version_re, version_file).group("version")
    return version


def generate_stub():
    import grpc_tools.protoc

    proto_include = pkg_resources.resource_filename("grpc_tools", "_proto")

    pb_dir = os.path.join(DIR, "src", PKG_NAME, "pb")
    if not os.path.exists(pb_dir):
        os.mkdir(pb_dir)

    for file in PROTO_FILES:
        print(PROTO_PATH, PROTO_FILES)
        res = grpc_tools.protoc.main(
            [
                "grpc_tools.protoc",
                f"-I{proto_include}",
                f"--proto_path={PROTO_PATH}",
                f"--python_out=src/{PKG_NAME}/pb",
                f"--grpc_python_out=src/{PKG_NAME}/pb",
                f"{file}",
            ]
        )
        if res != 0:
            print(f"Proto file generation failed. Cannot continue. Error code: {res}")
            exit(1)


class BuildPackage(build_py):
    def run(self):
        generate_stub()
        super(BuildPackage, self).run()


setup(
    name=PKG_NAME,
    version=find_version(),
    packages=find_packages(where="src"),
    description="Client for BastionLab Confidential Analytics.",
    long_description_content_type="text/markdown",
    keywords="confidential computing training client enclave amd-sev machine learning",
    cmdclass={"build_py": BuildPackage},
    long_description=LONG_DESCRIPTION,
    author="Kwabena Amponsem, Lucas Bourtoule",
    author_email="kwabena.amponsem@mithrilsecurity.io, luacs.bourtoule@nithrilsecurity.io",
    classifiers=["Programming Language :: Python :: 3"],
    install_requires=[
        "polars==0.14.24",
        "torch==1.12.1",
        "typing-extensions==4.4.0",
        "grpcio==1.47.0",
        "grpcio-tools==1.47.0",
        "pybind11==2.10.0",
    ],
    package_dir={"": "src"},
)
