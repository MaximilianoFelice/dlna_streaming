#!/usr/bin/python
# -*- coding: utf-8 -*-

# Present a live capture of the desktop as a file to a DLNA streaming server. 
# Copyright 2011 Michael Fötsch <foetsch@yahoo.com>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   1. Redistributions of source code must retain the above copyright notice,
#      this list of conditions and the following disclaimer.
#   2. Redistributions in binary form must reproduce the above copyright notice,
#      this list of conditions and the following disclaimer in the documentation
#      and/or other materials provided with the distribution.
#   3. The name of the author may not be used to endorse or promote products
#      derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR IMPLIED
# WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO
# EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import errno
import fuse
import os, string, re
import stat
import subprocess
import sys
import threading
import time
import mmap

fuse.fuse_python_api = (0, 2)

# The captured video is saved to a temporary file. TODO: Instead of writing
# to a file, keep some kind of FIFO queue in memory. As the player progresses
# through the file, throw out old parts of the file that it has already read.
# For now, storing a temporary file makes things easier to debug and keeps the
# code simple. 
TEMP_FILE = os.path.expanduser("./fuse_streaming.mkv")

class MyStat(fuse.Stat):
    def __init__(self):
        self.st_mode = stat.S_IFDIR | 0777
        self.st_ino = 0         # handled by FUSE
        self.st_dev = 0         # handled by FUSE
        self.st_nlink = 2       # a directory has two links: itself and ".."
        self.st_uid = 0
        self.st_gid = 0
        self.st_size = 4096
        self.st_atime = 0
        self.st_mtime = 0
        self.st_ctime = 0
        
# Producer thread that reads data from a stream, writes it to a file (if given),
# and notifies other threads via a condition variable.
class ReadThread(threading.Thread):
    class Output:
        def __init__(self, filename):
            self.fd = os.open(filename, os.O_CREAT | os.O_RDWR)
            self.step = 50 * 1024 * 1024
            self.size = self.step

            assert os.write(self.fd, '\x00' * self.size) == self.size

            self.outputFile = mmap.mmap(self.fd, self.size, mmap.MAP_PRIVATE, prot = mmap.PROT_READ | mmap.PROT_WRITE)
            self.fileSize = 0
            self.goal = 0
    
    def __init__(self, process, strm, output, condition):
        threading.Thread.__init__(self)
        self.process = process
        self.strm = strm
        self.output = output
        self.flag = condition

    def run(self):
        factor = 20
        while True:

            # Checks if file (and the map) needs to be resized #
            if (self.output.fileSize + (4096*factor)) > self.output.size:
                print "Resizing file"
                self.output.size += self.output.step
                self.output.outputFile.resize(self.output.size)
            
            # Reading data from disk #
            data = self.strm.read(4096*factor)
            self.output.outputFile.seek(self.output.fileSize)
            self.output.outputFile.write(data)

            # Updating fileSize #
            self.output.fileSize += len(data)

            # Setting flag if condition is met #
            if self.output.fileSize >= self.output.goal:
                self.flag.set()

class DlnaFuse(fuse.Fuse):
    def __init__(self, *args, **kw):
        fuse.Fuse.__init__(self, *args, **kw)

        self.needsMoreData = threading.Event()

        print "Main acquired"
        
        # There might be a slightly better way to do this, but we must create a file
        # if it doesn't exist. Still, we don't want it to be held open for writing
        # purposes. That might lead to the SO blocking it when we get access.
        if os.path.isfile(TEMP_FILE):
            os.remove(TEMP_FILE)

        aux = open(TEMP_FILE, "w+")
        aux.close()

        self.outputFile = ReadThread.Output(TEMP_FILE)
        
        live_filter = os.path.abspath(os.path.join(os.getcwd(), "matroska_live_filter.py"))

        cmd = ("ffmpeg -f alsa -ac 2 -i pulse -f x11grab -r 30 -s 1920x1080 -i :0.0 "
            "-acodec ac3 -ac 4 -vcodec libx264 "
            "-profile:v high422 -preset medium -level:v 3.1 -pix_fmt yuv420p -crf 35 -threads 0 "
            "-f matroska - | %(live_filter)s - ") % locals()

        print "Running capture command:", cmd
        self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        shell=True)
        
        self.recThread = ReadThread(self.process, self.process.stdout,
                      self.outputFile, self.needsMoreData)

        self.recThread.setDaemon(True)

    def getattr(self, path):
        print "getattr", path
        st = MyStat()
        st.st_atime = int(time.time())
        st.st_mtime = st.st_atime
        st.st_ctime = st.st_atime
        pe = path.split(os.path.sep)[1:]
        if path == "/":         # root of the FUSE filesystem
            pass
        elif len(pe) == 1 and pe[-1] == "fuse_live.mkv":
            st.st_mode = stat.S_IFREG | 0777
            st.st_nlink = 1
            st.st_size = 1024**3 #self.fileSize
        else:
            return -errno.ENOENT
        return st

    def readdir(self, path, offset):
        print "readdir", path, offset
        dirents = [".", ".."]
        if path == "/":
            dirents.extend(["fuse_live.mkv"])
        for r in dirents:
            yield fuse.Direntry(r)

    def mknod(self, path, mode, dev):
        print "mknod", path, mode, dev
        return -errno.EROFS

    def write(self, path, buf, offset):
        print "write", path, buf, offset
        return -errno.EROFS
    
    def read(self, path, size, offset):
        print "Read from ", path, "Offset: ", offset, "Size: ", size

        #If thread hasn't been started, we start it
        if not self.recThread.isAlive(): 
            print "Starting recording thread"
            self.recThread.start()

        # Check data availability
        self.outputFile.goal = offset + size
        if self.outputFile.fileSize < self.outputFile.goal:
            self.needsMoreData.clear()
            self.needsMoreData.wait()

        self.outputFile.outputFile.seek(offset)

        return self.outputFile.outputFile.read(size)

def main():
    server = DlnaFuse(version="%prog " + fuse.__version__,
                      usage="Run with './dlna_fuse -s -f -o allow_other <mount_point>' "
                            "to start live desktop capture",
                      dash_s_do="setsingle")
    server.parse(errex=1)
    server.main()

if __name__ == "__main__":
    main()
