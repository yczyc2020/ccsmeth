from __future__ import print_function
from setuptools import setup
import codecs
import os
import re

here = os.path.abspath(os.path.dirname(__file__))


# Get the version number from _version.py, and exe_path (learn from tombo)
verstrline = open(os.path.join(here, 'deepsmrt', '_version.py'), 'r').readlines()[-1]
vsre = r"^VERSION = ['\"]([^'\"]*)['\"]"
mo = re.search(vsre, verstrline)
if mo:
    __version__ = mo.group(1)
else:
    raise RuntimeError('Unable to find version string in "deepsmrt/_version.py".')


def read(*parts):
    # intentionally *not* adding an encoding option to open
    return codecs.open(os.path.join(here, *parts), 'r').read()


long_description = read('README.rst')


setup(
    name='none',
    packages=['nono'],
    keywords=['methylation', 'pacbio', 'neural network'],
    version=__version__,
    url='https://github.com/PengNi',
    download_url='https://github.com/PengNi//archive/{}.tar.gz'.format(__version__),
    license='GNU General Public License v3 (GPLv3)',
    author='Peng Ni',
    install_requires=['numpy>=1.15.3',
                      'statsmodels>=0.9.0',
                      'scikit-learn>=0.20.1',
                      'torch>=1.2.0,<=1.5',
                      ],
    author_email='543943952@qq.com',
    description='',
    long_description=long_description,
    entry_points={
        'console_scripts': [
            '=.:main',
            ],
        },
    platforms='any',
    zip_safe=False,
    include_package_data=True,
    classifiers=[
        'Programming Language :: Python :: 3',
        'Development Status :: 4 - Beta',
        'Natural Language :: English',
        'License :: OSI Approved :: GNU General Public License v3 (GPLv3)',
        'Operating System :: OS Independent',
        ],
)