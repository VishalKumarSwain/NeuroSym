#!/bin/bash
export GAN_TEST=1
/tmp/bitblast_solver_gan "$1" --esbmc-model --smtlib
