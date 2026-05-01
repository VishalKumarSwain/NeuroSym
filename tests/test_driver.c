/*
 * test_driver.c — GANSAT-generated test harness for Vp2-B2.c
 *
 * Strategy:
 *   - BOUND=2: each test case is a sequence of 2 inputs
 *   - Input domain: {1,2,3,4,5,6}  (observed from all (input==N) conditions)
 *   - GANSAT generates: all single values + cross-pairs that hit new branches
 *   - We run every generated test case and let gcov accumulate coverage
 *
 * Compile:
 *   gcc -DLLBMC -fprofile-arcs -ftest-coverage -O0 -lm \
 *       test_driver.c -o test_driver
 * Run:
 *   ./test_driver
 * Coverage:
 *   gcov -b -c Vp2-B2.c
 */

#define LLBMC          /* use stub path (#ifdef LLBMC) instead of klee */
#define BOUND 2

#include <stdio.h>
#include <string.h>

/* ── include stub before Vp2-B2.c to satisfy llbmc.h reference ── */
/* Vp2-B2.c uses #ifdef LLBMC → #include <llbmc.h>
   We redirect that include via -include flag in gcc, or define a shim here. */

/* Redirect llbmc.h: provide the one symbol LLBMC needs */
static inline void __llbmc_assume(int c) { (void)c; }
#define __llbmc_assert(c) ((void)(c))

/* ── external declarations from Vp2-B2.c ── */
extern int kappa;
extern int inputs[];
extern int a198, a85, a19, a9, a99, a33, a25, a74, a179;
extern int cf;
extern int input, output;

/* forward-declare the top-level entry point */
void calculate_output(int inp);
int  errorCheck(void);

/* ── Reset all globals to initial values before each test run ──
   Values copied from Vp2-B2.c global declarations.                */
extern int a136,a70,a44,a77,a188,a61,a58,a121,a153,a191,a103,a126;
extern int a4,a32,a19,a25,a82,a106,a173,a132,a72,a0,a158,a148;
extern int a42,a183,a23,a86,a33,a9,a64,a91,a194,a37,a102,a101;
extern int a156,a193,a181,a145,a54,a118,a99,a178,a78,a195,a149;
extern int a182,a135,a159,a115,a85,a176,a169,a140,a40,a179,a143;
extern int a174,a198,a170,a189,a107,a26,a116,a89,a114,a138,a177;
extern int a87,a79,a164,a157,a165,a34,a57,a35,a13,a68,a96,cf;
extern int a139,a1,a147,a60,a5,a90,a151,a16,a123,a197,a55,a124;
extern int a94,a2,a48,a74,a144,a63,a50,a47,a73,a122,a160,a92;
extern int a137,a7,a185,a192,a110,a53,a22,a111,a49,a150,a43,a27;
extern int a109,a104,a196,a152,a3;

void reset_globals(void) {
    a136=-73; a70=5;   a44=60;  a77=-46; a188=142; a61=10;  a58=3;
    a121=1;   a153=0;  a191=1;  a103=0;  a126=4;   a4=32;   a32=5;
    a19=16;   a25=457; a82=4;   a106=33; a173=32;  a132=12; a72=1;
    a0=9;     a158=5;  a148=5;  a42=11;  a183=4;   a23=32;  a86=2;
    a33=2;    a9=268;  a64=33;  a91=11;  a194=33;  a37=4;   a102=33;
    a101=32;  a156=1;  a193=1;  a181=300;a145=32;  a54=32;  a118=1;
    a99=186;  a178=32; a78=0;   a195=32; a149=1;   a182=67; a135=32;
    a159=0;   a115=32; a85=-9;  a176=34; a169=13;  a140=1;  a40=34;
    a179=206; a143=0;  a174=34; a198=380;a170=33;  a189=0;  a107=283;
    a26=0;    a116=1;  a89=33;  a114=1;  a138=32;  a177=1;  a87=237;
    a79=34;   a164=32; a157=10; a165=33; a34=33;   a57=10;  a35=33;
    a13=33;   a68=9;   a96=34;  cf=1;    a139=33;  a1=32;   a147=32;
    a60=34;   a5=1;    a90=-65; a151=32; a16=6;    a123=11; a197=34;
    a55=0;    a124=0;  a94=7;   a2=32;   a48=33;   a74=283; a144=0;
    a63=32;   a50=0;   a47=34;  a73=-130;a122=0;   a160=32; a92=0;
    a137=0;   a7=33;   a185=33; a192=201;a110=12;  a53=-88; a22=33;
    a111=0;   a49=0;   a150=34; a43=235; a27=7;    a109=5;  a104=32;
    a196=5;   a152=175;a3=32;   kappa=0;
}

/* ─────────────────────────────────────────────────────────────
 * GANSAT-generated test cases
 * Each test case = sequence of BOUND=2 inputs from {1..6}
 *
 * Generation strategy:
 *   - All 6 single-value pairs (i,i)
 *   - All 30 cross-pairs (i,j) where i≠j
 *   = 36 test cases total  → full product coverage of input domain
 * ───────────────────────────────────────────────────────────── */
static const int TEST_CASES[][2] = {
    /* single-value pairs */
    {1,1},{2,2},{3,3},{4,4},{5,5},{6,6},
    /* cross-pairs — each input followed by every other */
    {1,2},{1,3},{1,4},{1,5},{1,6},
    {2,1},{2,3},{2,4},{2,5},{2,6},
    {3,1},{3,2},{3,4},{3,5},{3,6},
    {4,1},{4,2},{4,3},{4,5},{4,6},
    {5,1},{5,2},{5,3},{5,4},{5,6},
    {6,1},{6,2},{6,3},{6,4},{6,5},
};
#define N_TESTS (sizeof(TEST_CASES) / sizeof(TEST_CASES[0]))

int main(void) {
    printf("========================================\n");
    printf("  GANSAT Test Driver — Vp2-B2.c\n");
    printf("  Test cases : %zu\n", N_TESTS);
    printf("  BOUND      : %d inputs per test\n", BOUND);
    printf("========================================\n\n");

    int passed = 0, errors = 0;

    for (int t = 0; t < (int)N_TESTS; t++) {
        reset_globals();

        int error_fired = 0;
        for (int step = 0; step < BOUND; step++) {
            int inp = TEST_CASES[t][step];
            calculate_output(inp);          /* cf reset to 1 at start of each call */
            if (cf == 0) error_fired = 1;   /* errorCheck fired this step — record but continue */
        }

        printf("  TC%02d [%d,%d] : %s\n",
               t + 1,
               TEST_CASES[t][0], TEST_CASES[t][1],
               error_fired ? "ERROR_PATH_HIT" : "PASS");

        if (!error_fired) passed++;
        else              errors++;
    }

    printf("\n========================================\n");
    printf("  Results: %d PASS  |  %d ERROR_PATH\n", passed, errors);
    printf("  Total  : %zu test cases executed\n", N_TESTS);
    printf("========================================\n");
    printf("\nNow run: gcov -b -c Vp2-B2.c\n");
    return 0;
}
