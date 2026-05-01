/*
 * sample.c — Test program for GANSAT
 *
 * A realistic software testing scenario:
 * Multiple functions with integer path conditions.
 * GANSAT will extract path constraints and find inputs
 * that reach each target branch.
 *
 * University of Manchester — PhD Software Testing
 */

#include <stdio.h>

/* ────────────────────────────────────────────────
 * Function 1: Triangle classifier
 * Find inputs (a, b, c) that form an equilateral triangle
 * Path: a==b AND b==c AND a>0
 * ──────────────────────────────────────────────── */
int classify_triangle(int a, int b, int c) {
    if (a <= 0 || b <= 0 || c <= 0)      return -1; /* invalid */
    if (a + b <= c || a + c <= b || b + c <= a) return 0; /* not a triangle */
    if (a == b && b == c)                 return 3; /* equilateral  ← TARGET */
    if (a == b || b == c || a == c)       return 2; /* isosceles */
    return 1;                                        /* scalene */
}

/* ────────────────────────────────────────────────
 * Function 2: Loan eligibility checker
 * Find (age, salary, credit_score) that gets approved
 * Path: age>=18 AND age<=65 AND salary>=30000
 *       AND credit_score>=700 AND salary+credit_score*10 >= 37000
 * ──────────────────────────────────────────────── */
int check_loan(int age, int salary, int credit_score) {
    if (age < 18 || age > 65)             return 0; /* age out of range */
    if (salary < 30000)                   return 0; /* insufficient salary */
    if (credit_score < 600)               return 0; /* bad credit */
    if (credit_score >= 700 && salary + credit_score * 10 >= 37000)
                                          return 2; /* approved ← TARGET */
    if (credit_score >= 650)              return 1; /* conditional */
    return 0;
}

/* ────────────────────────────────────────────────
 * Function 3: Scheduling conflict detector
 * Find (start1, end1, start2, end2) with NO overlap
 * Path: end1 <= start2 OR end2 <= start1
 * ──────────────────────────────────────────────── */
int has_conflict(int start1, int end1, int start2, int end2) {
    if (end1 <= start1 || end2 <= start2) return -1; /* invalid intervals */
    if (end1 <= start2 || end2 <= start1) return 0;  /* no conflict ← TARGET */
    return 1;                                         /* conflict */
}

/* ────────────────────────────────────────────────
 * Function 4: Buffer overflow boundary check
 * Find (index, size, offset) that passes all guards safely
 * Path: index>=0 AND index<size AND index+offset < size AND offset>=0
 * ──────────────────────────────────────────────── */
int safe_access(int index, int size, int offset) {
    if (size <= 0)                        return -1;
    if (index < 0 || index >= size)       return -1; /* out of bounds */
    if (offset < 0)                       return -1;
    if (index + offset >= size)           return -1; /* overflow ← avoid */
    return index + offset;                            /* safe access ← TARGET */
}

/* ────────────────────────────────────────────────
 * Main: run all with Z3-found test inputs
 * (actual values filled in by GANSAT solutions)
 * ──────────────────────────────────────────────── */
int main(void) {
    /* These values would be provided by GANSAT */
    printf("classify_triangle(5,5,5) = %d (expect 3)\n", classify_triangle(5,5,5));
    printf("check_loan(30,35000,720) = %d (expect 2)\n", check_loan(30,35000,720));
    printf("has_conflict(1,3,4,6)   = %d (expect 0)\n", has_conflict(1,3,4,6));
    printf("safe_access(2,10,5)     = %d (expect 7)\n", safe_access(2,10,5));
    return 0;
}
