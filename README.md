# salt-altlinux
Salt modules for ALT Linux

- altpkg.py: basic functionality tested and working.
  Doesn't work HOLD and UNHOLD.

  Because of APT/RPM package management system in ALT Linux this module 
  was combined from two package management salt modules :
    - aptpkg.py
    - yumpkg.py
