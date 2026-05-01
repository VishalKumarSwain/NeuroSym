/*
 * klee_stub.h — Stub KLEE functions for gcov compilation.
 * Replaces klee/klee.h so Vp2-B2.c compiles without KLEE installed.
 */
#ifndef KLEE_STUB_H
#define KLEE_STUB_H

#include <string.h>

/* klee_make_symbolic: in stub mode, value stays as-is (set by driver) */
static inline void klee_make_symbolic(void *addr, unsigned nbytes, const char *name) {
    (void)addr; (void)nbytes; (void)name;
}

static inline void klee_assume(int cond)       { (void)cond; }
static inline int  klee_int(const char *name)  { (void)name; return 0; }

#endif /* KLEE_STUB_H */
