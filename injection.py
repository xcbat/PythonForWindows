import struct
import ctypes
import os

import windows
import windows.utils as utils

from .native_exec import simple_x86 as x86
from .native_exec import simple_x64 as x64


def get_loadlib_getproc(target):
    if windows.current_process.bitness == target.bitness:
        LoadLibraryA = utils.get_func_addr('kernel32', 'LoadLibraryA')
        GetProcAddress = utils.get_func_addr('kernel32', 'GetProcAddress')
        return LoadLibraryA, GetProcAddress
    else:
        k32 = [x for x in target.peb.modules if x.name == "kernel32.dll"][0]
        exp = k32.pe.exports
        return exp['LoadLibraryA'], exp['GetProcAddress']


# 32 to 32 injection
def generate_python_exec_shellcode_32(target, PYDLL_addr, PyInit, PyRun, PYCODE_ADDR):
    LoadLibraryA, GetProcAddress = get_loadlib_getproc(target)
    code = x86.MultipleInstr()
    # Load python27.dll
    code += x86.Push(PYDLL_addr)
    code += x86.Mov('EAX', LoadLibraryA)
    code += x86.Call('EAX')
    # Get PyInit function into pythondll
    code += x86.Push('EAX')
    code += x86.Pop('EDI')
    code += x86.Push(PyInit)
    code += x86.Push('EDI')
    code += x86.Mov('EBX', GetProcAddress)
    code += x86.Call('EBX')
    # Call PyInit
    code += x86.Call('EAX')
    # Get PyRun function into pythondll
    code += x86.Push(PyRun)
    code += x86.Push('EDI')
    code += x86.Call('EBX')
    # Call PyRun with python code to exec
    code += x86.Push(PYCODE_ADDR)
    code += x86.Call('EAX')
    code += x86.Pop('EDI')
    code += x86.Ret()
    return code.get_code()


# 64 to 64 injection
def generate_python_exec_shellcode_64(target, PYDLL_addr, PyInit, PyRun, PYCODE_ADDR):

    LoadLibraryA, GetProcAddress = get_loadlib_getproc(target)

    Reserve_space_for_call = x64.MultipleInstr([x64.Push('RDI')] * 4)
    Clean_space_for_call = x64.MultipleInstr([x64.Pop('RDI')] * 4)

    code = x64.MultipleInstr()
    # Do stack alignement
    code += x64.Push('RCX')
    # Load python27.dll
    code += x64.Mov('RCX', PYDLL_addr)
    code += x64.Mov('RAX', LoadLibraryA)
    code += Reserve_space_for_call
    code += x64.Call('RAX')
    code += Clean_space_for_call
    code += x64.Push('RAX')
    code += x64.Pop('RCX')
    # Save RCX
    code += x64.Push('RCX')
    # Align stack
    code += x64.Push('RDI')
    # Get PyInit function into pythondll
    code += Reserve_space_for_call
    code += x64.Mov('RDX', PyInit)
    code += x64.Mov('RBX', GetProcAddress)
    code += x64.Call('RBX')
    # Call PyInit
    code += x64.Call('RAX')
    code += Clean_space_for_call
    # Remove Stack align
    code += x64.Pop('RDI')
    # Restore pythondll base into rcx
    code += x64.Pop('RCX')
    # Get PyRun function into pythondll
    code += x64.Mov('RDX', PyRun)
    code += Reserve_space_for_call
    code += x64.Call('RBX')
    # Call PyInit with python code to exec
    code += x64.Mov('RCX', PYCODE_ADDR)
    code += x64.Call('RAX')
    code += Clean_space_for_call
    # Remove stack alignement
    code += x64.Pop('RCX')
    code += x64.Ret()
    return code.get_code()


def inject_python_command(process, code_injected, PYDLL="python27.dll\x00"):
    PyInitT = "Py_Initialize\x00"
    Pyrun = "PyRun_SimpleString\x00"
    PYCODE = code_injected + "\x00"
    remote_addr_base = process.virtual_alloc(len(code_injected) + 0x100)
    remote_addr = remote_addr_base

    PYDLL_addr = remote_addr
    process.write_memory(remote_addr, PYDLL)
    remote_addr += len(PYDLL)

    PyInitT_ADDR = remote_addr
    process.write_memory(remote_addr, PyInitT)
    remote_addr += len(PyInitT)

    Pyrun_ADDR = remote_addr
    process.write_memory(remote_addr, Pyrun)
    remote_addr += len(Pyrun)

    PYCODE_ADDR = remote_addr
    process.write_memory(remote_addr, PYCODE)
    remote_addr += len(PYCODE)

    SHELLCODE_ADDR = remote_addr
    if process.bitness == 32:
        shellcode_generator = generate_python_exec_shellcode_32
    else:
        shellcode_generator = generate_python_exec_shellcode_64
    shellcode = shellcode_generator(process, PYDLL_addr, PyInitT_ADDR, Pyrun_ADDR, PYCODE_ADDR)
    process.write_memory(SHELLCODE_ADDR, shellcode)
    return SHELLCODE_ADDR


def validate_python_dll_presence(process):
    if windows.current_process.bitness == process.bitness:
        return True
    if windows.current_process.bitness == 32 and process.bitness == 64:
        with windows.utils.DisableWow64FsRedirection():
            if not os.path.exists(r"C:\system32\python27.dll"):
                raise ValueError("Could not find Python DLL to inject")
            return True
    if windows.current_process.bitness == 64 and process.bitness == 32:
        if not os.path.exists(r"C:\Windows\SysWOW64\python27.dll"):
            raise ValueError("Could not find Python DLL to inject")
        return True
    raise NotImplementedError("Unknown bitness")

def execute_python_code(process, code):
    validate_python_dll_presence(process)
    shellcode_remote_addr = inject_python_command(process, code)
    return process.create_thread(shellcode_remote_addr, 0)

retrieve_exc = r"""
import traceback
import sys
addr = {0}
txt = "".join(traceback.format_exception(sys.last_type, sys.last_value, sys.last_traceback))
import ctypes

size = ctypes.c_uint.from_address(addr)
size.value = len(txt)
buff = (ctypes.c_char * len(txt)).from_address(addr + ctypes.sizeof(ctypes.c_uint))
buff[:] = txt
"""

def retrieve_last_exception_data(process):
    # TODO : FREE THIS
    mem = process.virtual_alloc(0x1000)
    execute_python_code(process, retrieve_exc.format(mem))
    size = struct.unpack("<I", process.read_memory(mem, ctypes.sizeof(ctypes.c_uint)))[0]
    data = process.read_memory(mem + ctypes.sizeof(ctypes.c_uint), size)
    return data

class RemotePythonError(Exception):
    pass

def safe_execute_python(process, code):
    t = execute_python_code(process, code)
    t.wait() # Wait terminaison of the thread
    if t.exit_code == 0:
        return True
    if t.exit_code != 0xffffffff:
        raise ValueError("Unknown exit code {0}".format(hex(t.exit_code)))
    data = retrieve_last_exception_data(process)
    raise RemotePythonError(data)


