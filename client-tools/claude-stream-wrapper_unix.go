//go:build !windows

package main

func protectChildProcesses() error { return nil }
