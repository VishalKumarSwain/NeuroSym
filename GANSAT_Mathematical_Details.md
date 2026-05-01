# GANSAT — Complete Mathematical Details
## Iterative Refinement GAN for SMT Solving

---

## 1. Problem Formulation

### 1.1 SMT Satisfiability (QF_LIA)

A QF_LIA formula $\varphi$ over integer variables $\mathbf{x} = (x_1, x_2, \ldots, x_n)$ is a conjunction of linear constraints:

$$\varphi(\mathbf{x}) = \bigwedge_{i=1}^{m} C_i(\mathbf{x})$$

Each constraint $C_i$ has one of four forms:

| Type | Form | Index |
|------|------|-------|
| $\leq$ | $\mathbf{a}_i^\top \mathbf{x} \leq b_i$ | 0 |
| $=$ | $\mathbf{a}_i^\top \mathbf{x} = b_i$ | 1 |
| $\geq$ | $\mathbf{a}_i^\top \mathbf{x} \geq b_i$ | 2 |
| $\neq$ | $\mathbf{a}_i^\top \mathbf{x} \neq b_i$ | 3 |

where $\mathbf{a}_i \in \mathbb{Z}^n$ is the coefficient vector and $b_i \in \mathbb{Z}$ is the right-hand side.

**SMT Decision Problem:**

$$\text{SAT}(\varphi) = \begin{cases} \text{sat} & \exists\, \mathbf{x}^* \in \mathbb{Z}^n \text{ s.t. } \varphi(\mathbf{x}^*) = \top \\ \text{unsat} & \forall\, \mathbf{x} \in \mathbb{Z}^n,\; \varphi(\mathbf{x}) = \bot \end{cases}$$

### 1.2 GANSAT Objective

Instead of searching for $\mathbf{x}^*$ symbolically, GANSAT **learns a generator**:

$$G_\theta : \mathcal{F} \times \mathcal{Z} \to [-1,1]^{d_x}$$

that maps a formula encoding $\mathbf{f} \in \mathcal{F}$ and noise $\mathbf{z} \sim \mathcal{N}(0, I)$ to a candidate assignment $\hat{\mathbf{x}} \in [-1,1]^{d_x}$, decoded back to integers via the variable bounds.

---

## 2. Formula Encoding

### 2.1 Normalization Constants

$$\kappa_b = 10^4 \quad \text{(bound clip)}, \qquad \kappa_c = 10^4 \quad \text{(coefficient clip)}$$

### 2.2 Variable Bounds Block

For each variable $x_j$, extract its lower bound $\ell_j$ and upper bound $u_j$ from the formula assertions.

$$\mathbf{f}^{\text{bounds}} \in \mathbb{R}^{2 d_x}, \qquad d_x = 64$$

$$f^{\text{bounds}}_{2j} = \frac{\text{clip}(\ell_j,\, -\kappa_b,\, \kappa_b)}{\kappa_b}, \quad f^{\text{bounds}}_{2j+1} = \frac{\text{clip}(u_j,\, -\kappa_b,\, \kappa_b)}{\kappa_b}$$

### 2.3 Constraint Matrix Block

For each constraint $i \in \{1,\ldots,m\}$, $m = 128$:

$$\mathbf{f}^{\text{row}}_i = \left[\; \frac{a_{i,1}}{\kappa_c},\; \ldots,\; \frac{a_{i,d_x}}{\kappa_c},\; \frac{b_i}{\kappa_b},\; t_i \;\right] \in \mathbb{R}^{d_x + 2}$$

where $t_i \in \{0, 1, 2, 3\}$ encodes the constraint type.

### 2.4 Full Formula Encoding

$$\mathbf{f} = \left[\mathbf{f}^{\text{bounds}} \;\Big|\; \text{vec}\!\left(\mathbf{f}^{\text{row}}_1, \ldots, \mathbf{f}^{\text{row}}_m\right)\right] \in \mathbb{R}^{d_f}$$

$$d_f = 2d_x + m(d_x + 2) = 2(64) + 128(66) = 128 + 8448 = \boxed{8576}$$

### 2.5 Assignment Encoding / Decoding

**Encode** (for training, integer → normalized):

$$\hat{x}_j = \text{clip}\!\left(\frac{x_j}{\kappa_b},\, -1,\, 1\right) \in [-1,1]$$

**Decode** (inference, normalized → integer):

$$x_j = \text{round}\!\left(\ell_j + \frac{\hat{x}_j + 1}{2} \cdot (u_j - \ell_j)\right) \in \mathbb{Z}$$

---

## 3. Residual Block

All major subnetworks use the same residual block $\text{ResBlock} : \mathbb{R}^d \to \mathbb{R}^d$:

$$\text{ResBlock}(\mathbf{h}) = \text{LeakyReLU}_{0.2}\!\Big(\mathbf{h} + \text{LN}\big(W_2\, \text{Drop}_{0.1}\!\left(\text{LeakyReLU}_{0.2}(\text{LN}(W_1 \mathbf{h} + \mathbf{b}_1))\right) + \mathbf{b}_2\big)\Big)$$

where:
- $W_1, W_2 \in \mathbb{R}^{d \times d}$ are learnable weight matrices
- $\text{LN}$ is Layer Normalization: $\text{LN}(\mathbf{h}) = \frac{\mathbf{h} - \mu}{\sigma + \epsilon} \odot \boldsymbol{\gamma} + \boldsymbol{\beta}$
- $\text{Drop}_{0.1}$ is dropout with $p=0.1$
- $\text{LeakyReLU}_{0.2}(z) = \max(0.2z,\, z)$
- Skip connection prevents vanishing gradients in deep networks

---

## 4. ViolationComputer

### 4.1 Constraint Matrix Extraction

From $\mathbf{f} \in \mathbb{R}^{d_f}$, reshape the constraint block:

$$\mathbf{F}^C = \mathbf{f}_{[128:]} \in \mathbb{R}^{m(d_x+2)} \xrightarrow{\text{reshape}} \mathbf{F}^C \in \mathbb{R}^{m \times (d_x+2)}$$

Extract components:

$$A = \mathbf{F}^C_{[:, :d_x]} \in \mathbb{R}^{m \times d_x} \quad \text{(normalized coefficients)}$$

$$\mathbf{b} = \mathbf{F}^C_{[:, d_x]} \in \mathbb{R}^m \quad \text{(normalized RHS)}$$

$$\mathbf{t} = \mathbf{F}^C_{[:, d_x+1]} \in \mathbb{R}^m \quad \text{(constraint type index)}$$

### 4.2 Linear Combination

Given current assignment $\hat{\mathbf{x}} \in [-1,1]^{d_x}$:

$$\mathbf{p} = A\hat{\mathbf{x}} \in \mathbb{R}^m \qquad \text{(predicted LHS, normalized)}$$

$$\boldsymbol{\delta} = \mathbf{p} - \mathbf{b} \in \mathbb{R}^m \qquad \text{(signed difference)}$$

### 4.3 Type Masks (soft, differentiable)

$$M^{\leq}_i = \mathbb{1}[t_i < 0.25], \quad M^{=}_i = \mathbb{1}[0.25 \leq t_i < 0.75], \quad M^{\geq}_i = \mathbb{1}[0.75 \leq t_i < 1.25]$$

### 4.4 Per-Constraint Violation Score

$$v_i = \underbrace{\text{ReLU}(\delta_i) \cdot M^{\leq}_i}_{\text{violated if } p_i > b_i} + \underbrace{|\delta_i| \cdot M^{=}_i}_{\text{violated if } p_i \neq b_i} + \underbrace{\text{ReLU}(-\delta_i) \cdot M^{\geq}_i}_{\text{violated if } p_i < b_i}$$

$$\boxed{\mathbf{v}^C = (v_1, \ldots, v_m) \in \mathbb{R}^m_{\geq 0}} \qquad \text{(constraint violation vector)}$$

### 4.5 Per-Variable Violation Score

How responsible is each variable for the total violation?

$$\boxed{\mathbf{v}^V = \mathbf{v}^C \cdot |A| \in \mathbb{R}^{d_x}_{\geq 0}} \qquad \text{i.e.,}\quad v^V_j = \sum_{i=1}^m v^C_i \cdot |a_{ij}|}$$

Variables appearing in many violated constraints with large coefficients receive high scores — the refinement step targets these specifically.

### 4.6 Total Violation Score (scalar, used in loss)

$$\mathcal{V}(\mathbf{f}, \hat{\mathbf{x}}) = \sum_{i=1}^m v^C_i = \|\mathbf{v}^C\|_1$$

---

## 5. InitialGuesser

**Input:** $[\mathbf{f} \| \mathbf{z}] \in \mathbb{R}^{d_f + d_z}$ where $d_z = 128$, $\mathbf{z} \sim \mathcal{N}(0, I_{128})$

$$\mathbf{h}_0 = \text{LeakyReLU}_{0.2}(\text{LN}(W_{\text{in}}[\mathbf{f} \| \mathbf{z}] + \mathbf{b}_{\text{in}})) \in \mathbb{R}^{512}$$

$$\mathbf{h}_k = \text{ResBlock}(\mathbf{h}_{k-1}), \quad k = 1, 2, 3, 4$$

$$\hat{\mathbf{x}}^{(0)} = \tanh(W_{\text{out}}\mathbf{h}_4 + \mathbf{b}_{\text{out}}) \in [-1,1]^{d_x}$$

**Layer dimensions:**

$$\mathbb{R}^{8704} \xrightarrow{W_{\text{in}}} \mathbb{R}^{512} \xrightarrow{\times 4\,\text{ResBlock}} \mathbb{R}^{512} \xrightarrow{W_{\text{out}}} \mathbb{R}^{64} \xrightarrow{\tanh} [-1,1]^{64}$$

---

## 6. RefinementStep

### 6.1 Input Construction

$$\mathbf{r} = [\mathbf{f} \| \hat{\mathbf{x}}^{(k)} \| \mathbf{v}^C \| \mathbf{v}^V] \in \mathbb{R}^{d_r}$$

$$d_r = d_f + d_x + m + d_x = 8576 + 64 + 128 + 64 = \boxed{8832}$$

### 6.2 Forward Pass

$$\mathbf{h}_0 = \text{LeakyReLU}_{0.2}(\text{LN}(W_{\text{in}}\mathbf{r} + \mathbf{b}_{\text{in}})) \in \mathbb{R}^{256}$$

$$\mathbf{h}_k = \text{ResBlock}(\mathbf{h}_{k-1}), \quad k = 1, 2, 3$$

$$\boldsymbol{\Delta}^{(k)} = \tanh(W_{\text{out}}\mathbf{h}_3 + \mathbf{b}_{\text{out}}) \odot \mathbf{s} \in \mathbb{R}^{d_x}$$

where $\mathbf{s} \in \mathbb{R}^{d_x}$ is a **learned per-variable step size**, initialized to $0.1$.

### 6.3 Residual Update

$$\hat{\mathbf{x}}^{(k+1)} = \text{clip}\!\left(\hat{\mathbf{x}}^{(k)} + \boldsymbol{\Delta}^{(k)},\; -1,\; 1\right)$$

The **residual design** is critical: the network predicts a *correction* $\boldsymbol{\Delta}$, not an absolute assignment. This means:
- If $\boldsymbol{\Delta}^{(k)} \approx \mathbf{0}$, the assignment is already good
- The network only adjusts what needs fixing
- Gradients flow cleanly back to variables in violated constraints

**Layer dimensions:**

$$\mathbb{R}^{8832} \xrightarrow{W_{\text{in}}} \mathbb{R}^{256} \xrightarrow{\times 3\,\text{ResBlock}} \mathbb{R}^{256} \xrightarrow{W_{\text{out}}} \mathbb{R}^{64} \xrightarrow{\tanh \odot \mathbf{s}} \mathbb{R}^{64}$$

---

## 7. IterativeGenerator — Full Forward Pass

Let $K = 3$ be the number of refinement rounds.

$$\hat{\mathbf{x}}^{(0)} = \text{InitialGuesser}(\mathbf{f},\, \mathbf{z}), \qquad \mathbf{z} \sim \mathcal{N}(0, I_{128})$$

For $k = 0, 1, 2$:

$$\mathbf{v}^C_{(k)},\; \mathbf{v}^V_{(k)} = \text{ViolationComputer}(\mathbf{f},\, \hat{\mathbf{x}}^{(k)})$$

$$\hat{\mathbf{x}}^{(k+1)} = \text{RefinementStep}\!\left(\mathbf{f},\; \hat{\mathbf{x}}^{(k)},\; \mathbf{v}^C_{(k)},\; \mathbf{v}^V_{(k)}\right)$$

**Output:** $\hat{\mathbf{x}}^{(3)} \in [-1,1]^{64}$

### 7.1 Trajectory Visualization

```
Round 0:   x̂⁽⁰⁾ = InitialGuesser(f, z)        ← blind guess from distribution
              ↓ ViolationComputer
           v⁽⁰⁾_C, v⁽⁰⁾_V                        ← which constraints are violated?
              ↓ RefinementStep
Round 1:   x̂⁽¹⁾ = x̂⁽⁰⁾ + Δ⁽⁰⁾                 ← targeted correction
              ↓ ViolationComputer
           v⁽¹⁾_C, v⁽¹⁾_V                        ← fewer violations now
              ↓ RefinementStep
Round 2:   x̂⁽²⁾ = x̂⁽¹⁾ + Δ⁽¹⁾                 ← refined further
              ↓ ViolationComputer
           v⁽²⁾_C, v⁽²⁾_V
              ↓ RefinementStep
Round 3:   x̂⁽³⁾ = x̂⁽²⁾ + Δ⁽²⁾                 ← final output
```

### 7.2 Shared Weights Justification

The **same** RefinementStep weights are used in all 3 rounds. This is justified because:
- Each round solves the same task: "given violations, improve assignment"
- Shared weights force the network to learn a general refinement strategy
- Reduces parameters from $3 \times |\theta_{\text{refine}}|$ to $|\theta_{\text{refine}}|$
- Similar to weight sharing in recurrent networks (LSTM, GRU)

---

## 8. Discriminator

### 8.1 Input Construction

The Discriminator is **violation-aware** — it receives both the assignment and its violation signal:

$$\mathbf{d} = [\mathbf{f} \| \hat{\mathbf{x}} \| \mathbf{v}^C \| \mathbf{v}^V] \in \mathbb{R}^{d_r} = \mathbb{R}^{8832}$$

where $\mathbf{v}^C, \mathbf{v}^V = \text{ViolationComputer}(\mathbf{f}, \hat{\mathbf{x}})$ are computed internally.

### 8.2 Forward Pass

$$\mathbf{h}_0 = \text{LeakyReLU}_{0.2}(\text{LN}(W_{\text{in}}\mathbf{d} + \mathbf{b}_{\text{in}})) \in \mathbb{R}^{512}$$

$$\mathbf{h}_k = \text{ResBlock}(\mathbf{h}_{k-1}), \quad k = 1, 2, 3, 4$$

$$\ell = W_{\text{out}}\mathbf{h}_4 + b_{\text{out}} \in \mathbb{R} \qquad \text{(raw logit)}$$

$$D_\phi(\mathbf{f}, \hat{\mathbf{x}}) = \sigma(\ell) \in (0,1) \qquad \text{(probability of being satisfying)}$$

**Layer dimensions:**

$$\mathbb{R}^{8832} \xrightarrow{W_{\text{in}}} \mathbb{R}^{512} \xrightarrow{\times 4\,\text{ResBlock}} \mathbb{R}^{512} \xrightarrow{W_{\text{out}}} \mathbb{R}^1$$

### 8.3 Why Violation-Aware?

A standard Discriminator sees $(\mathbf{f}, \hat{\mathbf{x}})$ and must learn from scratch that satisfying assignments have zero violation. By providing $\mathbf{v}^C$ explicitly:

- Discriminator immediately "sees" whether $\hat{\mathbf{x}}$ violates any constraint
- Training converges faster — the signal is dense, not sparse
- Discriminator learns to penalize assignments based on *which* constraints are violated, not just whether the pair looks "right"

---

## 9. Training Objective

### 9.1 Dataset

Training set $\mathcal{D} = \{(\mathbf{f}_i, \mathbf{x}^*_i)\}_{i=1}^N$ where:
- $\mathbf{f}_i$ = encoded formula
- $\mathbf{x}^*_i$ = satisfying assignment found by Z3 (encoded to $[-1,1]^{d_x}$)

Only **SAT instances** are used — UNSAT instances have no positive training signal.

### 9.2 Discriminator Loss

Binary Cross-Entropy over real and fake pairs:

$$\mathcal{L}_D = -\underbrace{\mathbb{E}_{(\mathbf{f}, \mathbf{x}^*) \sim \mathcal{D}}\left[\log D_\phi(\mathbf{f}, \mathbf{x}^*)\right]}_{\text{real pairs (label = 1)}} - \underbrace{\mathbb{E}_{\mathbf{f} \sim \mathcal{D},\, \mathbf{z} \sim \mathcal{N}}\left[\log(1 - D_\phi(\mathbf{f}, G_\theta(\mathbf{f}, \mathbf{z})))\right]}_{\text{fake pairs (label = 0)}}$$

In practice, computed as the mean of two BCE losses:

$$\mathcal{L}_D = \frac{1}{2}\Big[\text{BCE}(D_\phi(\mathbf{f}, \mathbf{x}^*),\; \mathbf{1}) + \text{BCE}(D_\phi(\mathbf{f}, G_\theta(\mathbf{f}, \mathbf{z})),\; \mathbf{0})\Big]$$

### 9.3 Generator Loss (Novel: Dual Objective)

The generator minimizes **two terms simultaneously**:

#### Term 1: Adversarial Loss (fool the Discriminator)

$$\mathcal{L}_G^{\text{adv}} = -\mathbb{E}_{\mathbf{f} \sim \mathcal{D},\, \mathbf{z} \sim \mathcal{N}}\left[\log D_\phi(\mathbf{f}, G_\theta(\mathbf{f}, \mathbf{z}))\right]$$

$$= \text{BCE}(D_\phi(\mathbf{f}, G_\theta(\mathbf{f}, \mathbf{z})),\; \mathbf{1})$$

#### Term 2: Constraint Violation Loss (domain-specific, novel)

$$\mathcal{L}_G^{\text{viol}} = \mathbb{E}_{\mathbf{f} \sim \mathcal{D},\, \mathbf{z} \sim \mathcal{N}}\left[\mathcal{V}(\mathbf{f},\, G_\theta(\mathbf{f}, \mathbf{z}))\right] = \mathbb{E}\left[\,\|\mathbf{v}^C\|_1\,\right]$$

#### Combined Generator Loss

$$\boxed{\mathcal{L}_G = \mathcal{L}_G^{\text{adv}} + \lambda \cdot \mathcal{L}_G^{\text{viol}}, \qquad \lambda = 0.5}$$

### 9.4 Comparison with Standard cGAN

| | Standard cGAN | GANSAT (ours) |
|---|---|---|
| $\mathcal{L}_D$ | BCE on real/fake | BCE on real/fake |
| $\mathcal{L}_G$ | Adversarial only | Adversarial **+ Violation** |
| D input | $(f, \hat{x})$ | $(f, \hat{x}, v^C, v^V)$ |
| G architecture | One-shot MLP | Iterative refinement |
| Domain knowledge | None | Constraint structure |

The violation loss provides a **dense gradient signal** directly from the constraint structure — every training step, the generator receives feedback on exactly which constraints its output violates and by how much.

### 9.5 Training Schedule

| Phase | Steps per batch | Learning rate |
|---|---|---|
| Discriminator update | 2 | $10^{-4}$, Adam ($\beta_1=0.5$) |
| Generator update | 1 | $10^{-4}$, Adam ($\beta_1=0.5$) |

The 2:1 D:G update ratio prevents generator collapse — common practice in GAN training.

---

## 10. Gradient Flow Analysis

### 10.1 Through the Violation Loss

The key novelty is that $\mathcal{L}_G^{\text{viol}}$ provides gradients **through the constraint structure**:

$$\frac{\partial \mathcal{L}_G^{\text{viol}}}{\partial \hat{x}_j} = \sum_{i=1}^m \frac{\partial v^C_i}{\partial \hat{x}_j}$$

For a $\leq$ constraint: $v^C_i = \text{ReLU}(\mathbf{a}_i^\top \hat{\mathbf{x}} - b_i)$

$$\frac{\partial v^C_i}{\partial \hat{x}_j} = a_{ij} \cdot \mathbb{1}[\mathbf{a}_i^\top \hat{\mathbf{x}} > b_i]$$

This means: if constraint $i$ is violated, the gradient pushes $\hat{x}_j$ in direction $-a_{ij}$ — exactly the direction that reduces the violation. This is **projected gradient descent** learned end-to-end.

### 10.2 Through Refinement Rounds

With $K=3$ rounds, gradients flow back through all refinement steps via backpropagation through time (BPTT):

$$\frac{\partial \mathcal{L}_G}{\partial \theta_{\text{refine}}} = \sum_{k=0}^{K-1} \frac{\partial \mathcal{L}_G}{\partial \hat{\mathbf{x}}^{(K)}} \cdot \prod_{j=k}^{K-1} \frac{\partial \hat{\mathbf{x}}^{(j+1)}}{\partial \hat{\mathbf{x}}^{(j)}} \cdot \frac{\partial \hat{\mathbf{x}}^{(k+1)}}{\partial \theta_{\text{refine}}}$$

The residual structure $\hat{\mathbf{x}}^{(k+1)} = \hat{\mathbf{x}}^{(k)} + \boldsymbol{\Delta}^{(k)}$ ensures:

$$\frac{\partial \hat{\mathbf{x}}^{(j+1)}}{\partial \hat{\mathbf{x}}^{(j)}} = I + \frac{\partial \boldsymbol{\Delta}^{(j)}}{\partial \hat{\mathbf{x}}^{(j)}} \approx I \quad \text{(near identity)}$$

This prevents vanishing/exploding gradients across refinement rounds.

---

## 11. Inference (Test Time)

### 11.1 Best-of-N Sampling

Generate $N=16$ candidates with different noise seeds:

$$\hat{\mathbf{x}}^{(K)}_1, \ldots, \hat{\mathbf{x}}^{(K)}_N = G_\theta(\mathbf{f},\, \mathbf{z}_1), \ldots, G_\theta(\mathbf{f},\, \mathbf{z}_N), \qquad \mathbf{z}_i \sim \mathcal{N}(0, I)$$

### 11.2 Z3 Verification (Differentiable → Symbolic Handoff)

For each candidate $\hat{\mathbf{x}}^{(K)}_i$, decode to integers:

$$\mathbf{x}^*_i = \text{decode}(\hat{\mathbf{x}}^{(K)}_i) \in \mathbb{Z}^n$$

Check via Z3 symbolic substitution:

$$\text{verify}(\mathbf{x}^*_i) = \bigwedge_{c=1}^m \text{z3.simplify}\!\Big(C_c\!\left[\mathbf{x} \leftarrow \mathbf{x}^*_i\right]\Big) = \top$$

**Time complexity:** $O(m)$ per candidate — pure evaluation, no search.

### 11.3 Decision Procedure

$$\text{result} = \begin{cases} \text{sat},\; \mathbf{x}^*_i & \exists\, i : \text{verify}(\mathbf{x}^*_i) = \top \quad \text{(fast path, ~2ms)} \\ \text{Z3 full solve} & \text{otherwise} \quad \text{(fallback, complete)} \end{cases}$$

**Soundness:** Z3 verification is exact — no false positives.

**Completeness:** Z3 fallback is always run if no candidate passes — GANSAT never returns `unknown` when Z3 returns `sat` or `unsat`.

---

## 12. Parameter Count

| Module | Parameters |
|---|---|
| InitialGuesser | $\approx 4.7M$ |
| RefinementStep (×1, shared) | $\approx 2.5M$ |
| ViolationComputer | $0$ (parameter-free) |
| Discriminator | $\approx 4.9M$ |
| **Generator total** | $\approx 7.2M$ |
| **Full model total** | $\approx 12.1M$ |

Calculation for InitialGuesser:
$$W_{\text{in}}: 8704 \times 512 = 4.5M, \quad 4 \times \text{ResBlock}: 4 \times 2 \times 512^2 \approx 2.1M, \quad W_{\text{out}}: 512 \times 64 = 32K$$

---

## 13. Complexity Summary

| Operation | Time complexity | Wall-clock (est.) |
|---|---|---|
| Formula encoding | $O(m \cdot n)$ | ~0.1ms |
| Generator forward (1 sample) | $O(K \cdot d_f \cdot h)$ | ~1ms |
| Z3 verification (1 candidate) | $O(m \cdot n)$ | ~0.1ms |
| Best-of-16 fast path | $O(N \cdot (K \cdot d_f h + mn))$ | ~3ms |
| Z3 full solve (fallback) | $O(\text{exp})$ worst case | 10–20s budget |

For formula families where the GAN fast path succeeds often (similar distribution to training data), expected speedup over Z3 is **2–10×**.

---

## 14. Summary Diagram

```
                    Training                          Inference
                    ────────                          ─────────

(f, x*)∈D ──────► Discriminator ◄─── G(f,z)    f ──► Encode ──► f̂
                      │                              f̂ ──► G_θ(f̂, z₁..z₁₆)
                    L_D ◄───────────────────────       = x̂₁..x̂₁₆
                      │                              x̂ᵢ ──► decode ──► x*ᵢ ∈ ℤⁿ
              ┌───────┘                              x*ᵢ ──► Z3.verify?
              │                                           ├── ✓ → sat, model
         ┌────▼────┐                                     └── ✗ → try next
         │  L_D    │  Discriminator loss
         └─────────┘

    G(f,z):
      z~N(0,I)
         │
    InitialGuesser(f,z)
         │  x̂⁽⁰⁾
    ┌────▼──────────────────────────────────┐  ×K=3
    │  ViolationComputer(f, x̂⁽ᵏ⁾)          │
    │    → v^C [128], v^V [64]             │
    │  RefinementStep(f, x̂⁽ᵏ⁾, v^C, v^V)  │
    │    → x̂⁽ᵏ⁺¹⁾ = x̂⁽ᵏ⁾ + Δ⁽ᵏ⁾          │
    └───────────────────────────────────────┘
         │  x̂⁽³⁾
    ┌────▼────────────────────────────────────────────┐
    │  L_G = BCE(D(f, x̂), 1) + 0.5·||v^C(f,x̂)||₁   │
    └─────────────────────────────────────────────────┘
              Adversarial loss + Violation loss
```

---

*Document: GANSAT Mathematical Details*
*University of Manchester — PhD Software Testing*
*SMT-COMP '26, QF_LIA Track*
