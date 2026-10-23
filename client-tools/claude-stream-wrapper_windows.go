package main

import (
	"fmt"
	"syscall"
	"unsafe"
)

type jobBasicLimits struct {
	ProcessTime, JobTime           int64
	LimitFlags                     uint32
	MinWorkingSet, MaxWorkingSet   uintptr
	ActiveProcesses                uint32
	Affinity                       uintptr
	PriorityClass, SchedulingClass uint32
}

type jobExtendedLimits struct {
	BasicLimits                                                jobBasicLimits
	IOCounters                                                 [6]uint64
	ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory uintptr
}

// A Windows SDK abort forcibly terminates the wrapper without delivering
// SIGTERM. Put this process and its future children in a kill-on-close job so
// the bundled engine exits too. The job handle is deliberately retained until
// OS process exit, and is not inheritable. Nested jobs work on modern Windows.
// https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
func protectChildProcesses() error {
	kernel := syscall.NewLazyDLL("kernel32.dll")
	job, _, err := kernel.NewProc("CreateJobObjectW").Call(0, 0)
	if job == 0 {
		return fmt.Errorf("create launcher job: %w", err)
	}
	limits := jobExtendedLimits{}
	limits.BasicLimits.LimitFlags = 0x2000 // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	ok, _, err := kernel.NewProc("SetInformationJobObject").Call(job, 9,
		uintptr(unsafe.Pointer(&limits)), unsafe.Sizeof(limits))
	if ok == 0 {
		_ = syscall.CloseHandle(syscall.Handle(job))
		return fmt.Errorf("configure launcher job: %w", err)
	}
	current, _, _ := kernel.NewProc("GetCurrentProcess").Call()
	ok, _, err = kernel.NewProc("AssignProcessToJobObject").Call(job, current)
	if ok == 0 {
		_ = syscall.CloseHandle(syscall.Handle(job))
		return fmt.Errorf("join launcher job: %w", err)
	}
	return nil
}
