// Standalone C++ QF_BV bit-blast + MiniSat solver for NeuroSym.
// Reads a JSON IR (produced by /tmp/dump_ir.py from the Python repo's own
// parser/AST) describing a DAG of BV/Bool terms, Tseitin-encodes it faithfully
// following gansat/ns_bitblaster.py's gate/arithmetic algorithms, hands the
// CNF to Minisat::SimpSolver, and reports sat/unsat plus variable models.
//
// No SMT-LIB2 parsing here (out of scope for this pass) -- the IR is the
// only real input format. See report for details.

#include <minisat/core/Solver.h>
#include <minisat/simp/SimpSolver.h>

#include <algorithm>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <chrono>
#include <fstream>
#include <iostream>
#include <map>
#include <sstream>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

using namespace Minisat;

// ─────────────────────────────── minimal JSON ──────────────────────────────
// Small hand-rolled JSON reader: enough for the flat object/array/number/
// string/bool shapes dump_ir.py emits. Not a general parser.

struct JValue {
    enum Type { NUL, BOOL, NUM, STR, ARR, OBJ } type = NUL;
    bool b = false;
    double num = 0;
    std::string numStr;  // raw digits, exact -- avoids double-precision loss
                          // for 64-bit bvlit values (e.g. 2^64-1)
    std::string str;
    std::vector<JValue> arr;
    std::map<std::string, JValue> obj;

    bool isNull() const { return type == NUL; }
    long long asInt() const {
        // Parse the raw digit string as unsigned 64-bit then reinterpret,
        // since bvlit values can be up to 2^width-1 which for width=64
        // exceeds what a double mantissa (53 bits) can represent exactly.
        if (!numStr.empty()) {
            unsigned long long u = strtoull(numStr.c_str(), nullptr, 10);
            return (long long)u;
        }
        return (long long)num;
    }
    bool asBool() const { return b; }
    const std::string &asStr() const { return str; }
};

struct JParser {
    const char *p, *end, *begin;
    JParser(const std::string &s) : p(s.c_str()), end(s.c_str() + s.size()), begin(s.c_str()) {}

    void skipWs() { while (p < end && (*p==' '||*p=='\t'||*p=='\n'||*p=='\r')) p++; }

    JValue parseValue() {
        skipWs();
        if (p >= end) throw std::runtime_error("unexpected end of JSON");
        char c = *p;
        if (c == '{') return parseObj();
        if (c == '[') return parseArr();
        if (c == '"') return parseStr();
        if (c == 't') { p += 4; JValue v; v.type = JValue::BOOL; v.b = true; return v; }
        if (c == 'f') { p += 5; JValue v; v.type = JValue::BOOL; v.b = false; return v; }
        if (c == 'n') { p += 4; JValue v; v.type = JValue::NUL; return v; }
        return parseNum();
    }

    JValue parseStr() {
        JValue v; v.type = JValue::STR;
        p++; // opening quote
        std::string s;
        while (p < end && *p != '"') {
            if (*p == '\\') {
                p++;
                if (p < end) { s.push_back(*p); p++; }
            } else {
                s.push_back(*p); p++;
            }
        }
        p++; // closing quote
        v.str = s;
        return v;
    }

    JValue parseNum() {
        const char *start = p;
        if (*p=='-'||*p=='+') p++;
        while (p < end && (isdigit((unsigned char)*p)||*p=='.'||*p=='e'||*p=='E'||*p=='+'||*p=='-')) p++;
        JValue v; v.type = JValue::NUM;
        v.numStr = std::string(start, p);
        try { v.num = std::stod(v.numStr); } catch (...) { v.num = 0; }
        return v;
    }

    JValue parseArr() {
        JValue v; v.type = JValue::ARR;
        p++; skipWs();
        if (*p == ']') { p++; return v; }
        while (true) {
            v.arr.push_back(parseValue());
            skipWs();
            if (*p == ',') { p++; continue; }
            if (*p == ']') { p++; break; }
            throw std::runtime_error("bad array at offset " + std::to_string(p - begin) +
                                      " char='" + std::string(1, p<end?*p:'?') + "'");
        }
        return v;
    }

    JValue parseObj() {
        JValue v; v.type = JValue::OBJ;
        p++; skipWs();
        if (*p == '}') { p++; return v; }
        while (true) {
            skipWs();
            JValue key = parseStr();
            skipWs();
            if (*p != ':') throw std::runtime_error("expected :");
            p++;
            JValue val = parseValue();
            v.obj[key.str] = val;
            skipWs();
            if (*p == ',') { p++; continue; }
            if (*p == '}') { p++; break; }
            throw std::runtime_error("bad object");
        }
        return v;
    }
};

static JValue parseJsonFile(const std::string &path) {
    std::ifstream f(path);
    if (!f) throw std::runtime_error("cannot open " + path);
    std::stringstream ss;
    ss << f.rdbuf();
    std::string contents = ss.str();  // must outlive the parser: JParser
                                       // stores raw pointers into this buffer
    JParser jp(contents);
    return jp.parseValue();
}

static const JValue *objGet(const JValue &o, const char *key) {
    auto it = o.obj.find(key);
    if (it == o.obj.end()) return nullptr;
    return &it->second;
}

// ─────────────────────────────── Tseitin core ───────────────────────────────

struct Blaster {
    SimpSolver &S;
    int nextVar = 0;      // 0-based minisat var allocation counter
    int constTrueLit = 0; // 1-based dimacs-style literal cache
    int constFalseLit = 0;
    bool haveTrue = false, haveFalse = false;
    // XOR-gate cache -- keyed by (min(a,b), max(a,b)) since gate_xor(a,b)
    // and gate_xor(b,a) produce identical clause sets (XOR is symmetric).
    // Scoped to this Blaster instance (fresh per formula solve), mirroring
    // the Python _gate_xor cache added to gansat/ns_bitblaster.py's _Alloc.
    std::map<std::pair<int,int>, int> xorCache;
    long xorCacheHits = 0, xorCacheMisses = 0;
    long xorVarsAvoided = 0, xorClausesAvoided = 0;
    // AND-gate cache -- keyed by (min(a,b), max(a,b)), mirroring the XOR
    // cache above. Measured on a real large ESBMC-generated pointer-
    // aliasing formula (captured_dirname.smt2, ~7M SAT vars) where AND
    // gates showed ~21% global SAT-literal-level duplication (650386 of
    // 3088831 calls) -- unlike smaller test formulas earlier this session
    // where AND/OR showed ~0% duplication and were correctly left uncached.
    // OR/ITE/eq were also measured on this same formula and stayed
    // negligible (<1%), so only AND gets a cache here.
    std::map<std::pair<int,int>, int> andCache;
    long andCacheHits = 0, andCacheMisses = 0;
    long andVarsAvoided = 0, andClausesAvoided = 0;

    Blaster(SimpSolver &s) : S(s) {}

    int fresh() {
        Var v = S.newVar();
        return (int)v + 1; // 1-based positive literal
    }

    void addClauseLits(const std::vector<int> &lits) {
        vec<Lit> ps;
        for (int l : lits) {
            int var = (l > 0 ? l : -l) - 1;
            ps.push(mkLit(var, l < 0));
        }
        S.addClause(ps);
    }

    int CONST_TRUE() {
        if (haveTrue) return constTrueLit;
        int y = fresh();
        addClauseLits({y});
        constTrueLit = y;
        haveTrue = true;
        return y;
    }
    int CONST_FALSE() {
        if (haveFalse) return constFalseLit;
        int y = fresh();
        addClauseLits({-y});
        constFalseLit = y;
        haveFalse = true;
        return y;
    }

    int gate_not(int a) { return -a; }

    int gate_and(int a, int b) {
        std::pair<int,int> key = (a <= b) ? std::make_pair(a, b) : std::make_pair(b, a);
        auto it = andCache.find(key);
        if (it != andCache.end()) {
            andCacheHits++;
            andVarsAvoided++;
            andClausesAvoided += 3;
            return it->second;
        }
        andCacheMisses++;
        int y = fresh();
        addClauseLits({-y, a});
        addClauseLits({-y, b});
        addClauseLits({y, -a, -b});
        andCache[key] = y;
        return y;
    }
    int gate_or(int a, int b) {
        int y = fresh();
        addClauseLits({y, -a});
        addClauseLits({y, -b});
        addClauseLits({-y, a, b});
        return y;
    }
    int gate_xor(int a, int b) {
        std::pair<int,int> key = (a <= b) ? std::make_pair(a, b) : std::make_pair(b, a);
        auto it = xorCache.find(key);
        if (it != xorCache.end()) {
            xorCacheHits++;
            xorVarsAvoided++;
            xorClausesAvoided += 4;
            return it->second;
        }
        xorCacheMisses++;
        int y = fresh();
        addClauseLits({-y, -a, -b});
        addClauseLits({-y, a, b});
        addClauseLits({y, -a, b});
        addClauseLits({y, a, -b});
        xorCache[key] = y;
        return y;
    }
    int gate_ite(int c, int t, int e) {
        int y = fresh();
        addClauseLits({-y, -c, t});
        addClauseLits({-y, c, e});
        addClauseLits({y, -c, -t});
        addClauseLits({y, c, -e});
        return y;
    }
    int gate_and_n(const std::vector<int> &bits) {
        if (bits.empty()) return CONST_TRUE();
        int acc = bits[0];
        for (size_t i = 1; i < bits.size(); i++) acc = gate_and(acc, bits[i]);
        return acc;
    }
    int gate_or_n(const std::vector<int> &bits) {
        if (bits.empty()) return CONST_FALSE();
        int acc = bits[0];
        for (size_t i = 1; i < bits.size(); i++) acc = gate_or(acc, bits[i]);
        return acc;
    }
    std::vector<int> int_to_bits(uint64_t val, int w) {
        uint64_t mask = (w >= 64) ? ~0ULL : ((1ULL << w) - 1);
        val &= mask;
        std::vector<int> bits(w);
        for (int i = 0; i < w; i++) {
            int shift = w - 1 - i; // MSB-first
            bool bit = (val >> shift) & 1ULL;
            bits[i] = bit ? CONST_TRUE() : CONST_FALSE();
        }
        return bits;
    }

    // ── arithmetic ──

    void full_adder(int a, int b, int cin, int &sum, int &cout) {
        int ab = gate_xor(a, b);
        sum = gate_xor(ab, cin);
        int c1 = gate_and(a, b);
        int c2 = gate_and(cin, ab);
        cout = gate_or(c1, c2);
    }

    std::vector<int> bv_add(const std::vector<int> &a, const std::vector<int> &b) {
        int w = a.size();
        std::vector<int> sums(w);
        int carry = CONST_FALSE();
        for (int i = w - 1; i >= 0; i--) {
            int s, c;
            full_adder(a[i], b[i], carry, s, c);
            sums[i] = s;
            carry = c;
        }
        return sums;
    }

    std::vector<int> bv_not(const std::vector<int> &a) {
        std::vector<int> r(a.size());
        for (size_t i = 0; i < a.size(); i++) r[i] = gate_not(a[i]);
        return r;
    }

    std::vector<int> bv_neg(const std::vector<int> &a) {
        int w = a.size();
        std::vector<int> not_a = bv_not(a);
        std::vector<int> one = int_to_bits(1, w);
        return bv_add(not_a, one);
    }

    std::vector<int> bv_sub(const std::vector<int> &a, const std::vector<int> &b) {
        return bv_add(a, bv_neg(b));
    }

    std::vector<int> bv_and(const std::vector<int> &a, const std::vector<int> &b) {
        std::vector<int> r(a.size());
        for (size_t i = 0; i < a.size(); i++) r[i] = gate_and(a[i], b[i]);
        return r;
    }
    std::vector<int> bv_or(const std::vector<int> &a, const std::vector<int> &b) {
        std::vector<int> r(a.size());
        for (size_t i = 0; i < a.size(); i++) r[i] = gate_or(a[i], b[i]);
        return r;
    }
    std::vector<int> bv_xor(const std::vector<int> &a, const std::vector<int> &b) {
        std::vector<int> r(a.size());
        for (size_t i = 0; i < a.size(); i++) r[i] = gate_xor(a[i], b[i]);
        return r;
    }

    std::vector<int> bv_mul(const std::vector<int> &a, const std::vector<int> &b) {
        int w = a.size();
        std::vector<int> result = int_to_bits(0, w);
        for (int i = w - 1; i >= 0; i--) {
            int shift = w - 1 - i;
            // Partial product for bit i of b (weight 2^shift) is a<<shift,
            // i.e. new position k takes old position k+shift (zero-filled
            // once k+shift runs off the LSB end at k >= w-shift) -- see
            // bv_shl_const just below, which this mirrors exactly. NOTE:
            // this was previously computed as a[k-shift] (0-filled at the
            // MSB end instead), which is a right shift, not a left shift --
            // a pre-existing correctness bug in the general (non-constant)
            // multiply path, found and fixed here via the differential
            // tests added for the mul/div strength-reduction work.
            std::vector<int> shifted(w);
            for (int k = 0; k < w - shift; k++) shifted[k] = a[k + shift];
            for (int k = w - shift; k < w; k++) shifted[k] = CONST_FALSE();
            std::vector<int> masked(w);
            for (int j = 0; j < w; j++) masked[j] = gate_and(shifted[j], b[i]);
            result = bv_add(result, masked);
        }
        return result;
    }

    // ── constant-shift-amount rewiring, used by the bvmul/bvudiv/bvurem
    // power-of-2 strength reductions below. Unlike bv_shl/bv_lshr (which
    // encode a *variable* shift amount via a gate_ite chain over the bits
    // of b), the shift amount here is a compile-time int, so this is pure
    // literal rewiring -- zero new gates/clauses/variables.
    std::vector<int> bv_shl_const(const std::vector<int> &a, int k) {
        int w = (int)a.size();
        std::vector<int> r(w);
        for (int i = 0; i < w; i++) {
            int srcIdx = i + k; // MSB-first: new bit i comes from old bit i+k
            r[i] = (srcIdx < w) ? a[srcIdx] : CONST_FALSE();
        }
        return r;
    }
    // General constant multiply via shift-add decomposition: for a known
    // constant C with popcount p (p >= 2; p==1 is the pow2 case handled
    // separately, and it's always a strict win over the general O(w^2)
    // schoolbook bv_mul below -- p-1 additions of w-bit shifted copies of
    // x, versus w additions each preceded by a per-bit AND gate against a
    // symbolic multiplicand bit. bv_shl_const is free (pure rewiring), so
    // this costs exactly (p-1) full bv_add calls, each O(w) gates/clauses,
    // total O(p*w) versus bv_mul's O(w^2) -- for the small-popcount
    // constants real ESBMC RERS output multiplies by (5, 10, 3, 9, ...:
    // popcount 2 each), this replaces ~31 additions plus w^2 AND-gated
    // partial products with a single addition and zero extra AND gates.
    // Found directly from a real corpus OOM: bit-blasting ~900+ variable*
    // small-constant multiplies with the general schoolbook multiplier
    // produced a CNF large enough to exhaust an 8GB MiniSat clause
    // database mid-search (Minisat::OutOfMemoryException) on 4 of 8 real
    // RERS B4 benchmarks that otherwise solve in seconds.
    std::vector<int> bv_mul_by_sparse_const(const std::vector<int> &x, uint64_t c, int w) {
        std::vector<int> acc;
        bool first = true;
        for (int i = 0; i < w; i++) {
            if (!((c >> i) & 1ULL)) continue;
            std::vector<int> shifted = bv_shl_const(x, i);
            if (first) { acc = shifted; first = false; }
            else acc = bv_add(acc, shifted);
        }
        return acc; // caller guarantees c != 0, so acc is always assigned
    }

    std::vector<int> bv_lshr_const(const std::vector<int> &a, int k) {
        int w = (int)a.size();
        std::vector<int> r(w);
        for (int i = 0; i < w; i++) r[i] = (i >= k) ? a[i - k] : CONST_FALSE();
        return r;
    }
    // Keep only the low k bits of a (MSB-first array -> low bits are the
    // last k entries), zero-filling the rest. Equivalent to a & (2^k - 1).
    std::vector<int> bv_urem_pow2_const(const std::vector<int> &a, int k) {
        int w = (int)a.size();
        std::vector<int> r(w);
        for (int i = 0; i < w; i++) r[i] = (i >= w - k) ? a[i] : CONST_FALSE();
        return r;
    }

    int bv_eq(const std::vector<int> &a, const std::vector<int> &b) {
        std::vector<int> eq_bits(a.size());
        for (size_t i = 0; i < a.size(); i++) eq_bits[i] = gate_not(gate_xor(a[i], b[i]));
        return gate_and_n(eq_bits);
    }

    int bv_ult(const std::vector<int> &a, const std::vector<int> &b) {
        int w = a.size();
        int borrow = CONST_FALSE();
        for (int i = w - 1; i >= 0; i--) {
            int ai = a[i], bi = b[i];
            int not_ai = gate_not(ai);
            int ai_lt_bi = gate_and(not_ai, bi);
            int eq_i = gate_not(gate_xor(ai, bi));
            int prop = gate_and(eq_i, borrow);
            borrow = gate_or(ai_lt_bi, prop);
        }
        return borrow;
    }
    int bv_ule(const std::vector<int> &a, const std::vector<int> &b) { return gate_not(bv_ult(b, a)); }

    int bv_slt(const std::vector<int> &a, const std::vector<int> &b) {
        int a_s = a[0], b_s = b[0];
        int not_b_s = gate_not(b_s);
        int diff_sign = gate_and(a_s, not_b_s);
        int same_sign = gate_not(gate_xor(a_s, b_s));
        int ult_rest = bv_ult(a, b);
        int both = gate_and(same_sign, ult_rest);
        return gate_or(diff_sign, both);
    }
    int bv_sle(const std::vector<int> &a, const std::vector<int> &b) { return gate_not(bv_slt(b, a)); }

    void bv_udivrem(const std::vector<int> &a, const std::vector<int> &b,
                     std::vector<int> &q_out, std::vector<int> &r_out) {
        int w = a.size();
        std::vector<int> r(w + 1);
        for (int i = 0; i < w + 1; i++) r[i] = CONST_FALSE();
        std::vector<int> b_ext(w + 1);
        b_ext[0] = CONST_FALSE();
        for (int i = 0; i < w; i++) b_ext[i + 1] = b[i];
        std::vector<int> q_bits;
        q_bits.reserve(w);
        for (int i = 0; i < w; i++) {
            std::vector<int> shifted(w + 1);
            for (int k = 0; k < w; k++) shifted[k] = r[k + 1];
            shifted[w] = a[i];
            r = shifted;
            int ge = gate_not(bv_ult(r, b_ext));
            std::vector<int> r_sub = bv_sub(r, b_ext);
            std::vector<int> r_new(w + 1);
            for (int k = 0; k < w + 1; k++) r_new[k] = gate_ite(ge, r_sub[k], r[k]);
            r = r_new;
            q_bits.push_back(ge);
        }
        q_out = q_bits;
        r_out = std::vector<int>(r.begin() + 1, r.end());
    }

    std::vector<int> bv_bvudiv(const std::vector<int> &a, const std::vector<int> &b) {
        int w = a.size();
        std::vector<int> q, r;
        bv_udivrem(a, b, q, r);
        int b_is_zero = gate_not(gate_or_n(b));
        std::vector<int> result(w);
        for (int k = 0; k < w; k++) result[k] = gate_ite(b_is_zero, CONST_TRUE(), q[k]);
        return result;
    }
    std::vector<int> bv_bvurem(const std::vector<int> &a, const std::vector<int> &b) {
        int w = a.size();
        std::vector<int> q, r;
        bv_udivrem(a, b, q, r);
        int b_is_zero = gate_not(gate_or_n(b));
        std::vector<int> result(w);
        for (int k = 0; k < w; k++) result[k] = gate_ite(b_is_zero, a[k], r[k]);
        return result;
    }

    std::vector<int> bv_bvsdiv(const std::vector<int> &a, const std::vector<int> &b) {
        int w = a.size();
        int a_sign = a[0], b_sign = b[0];
        std::vector<int> a_neg = bv_neg(a), b_neg = bv_neg(b);
        std::vector<int> a_mag(w), b_mag(w);
        for (int k = 0; k < w; k++) a_mag[k] = gate_ite(a_sign, a_neg[k], a[k]);
        for (int k = 0; k < w; k++) b_mag[k] = gate_ite(b_sign, b_neg[k], b[k]);
        std::vector<int> q_mag, r_mag;
        bv_udivrem(a_mag, b_mag, q_mag, r_mag);
        std::vector<int> q_neg = bv_neg(q_mag);
        int sign_differs = gate_xor(a_sign, b_sign);
        std::vector<int> result(w);
        for (int k = 0; k < w; k++) result[k] = gate_ite(sign_differs, q_neg[k], q_mag[k]);
        int b_is_zero = gate_not(gate_or_n(b));
        std::vector<int> one_val(w);
        for (int k = 0; k < w - 1; k++) one_val[k] = CONST_FALSE();
        one_val[w - 1] = CONST_TRUE();
        std::vector<int> zero_case(w);
        for (int k = 0; k < w; k++) zero_case[k] = gate_ite(a_sign, one_val[k], CONST_TRUE());
        std::vector<int> out(w);
        for (int k = 0; k < w; k++) out[k] = gate_ite(b_is_zero, zero_case[k], result[k]);
        return out;
    }

    std::vector<int> bv_bvsrem(const std::vector<int> &a, const std::vector<int> &b) {
        int w = a.size();
        int a_sign = a[0], b_sign = b[0];
        std::vector<int> a_neg = bv_neg(a), b_neg = bv_neg(b);
        std::vector<int> a_mag(w), b_mag(w);
        for (int k = 0; k < w; k++) a_mag[k] = gate_ite(a_sign, a_neg[k], a[k]);
        for (int k = 0; k < w; k++) b_mag[k] = gate_ite(b_sign, b_neg[k], b[k]);
        std::vector<int> q_mag, r_mag;
        bv_udivrem(a_mag, b_mag, q_mag, r_mag);
        std::vector<int> r_neg = bv_neg(r_mag);
        std::vector<int> result(w);
        for (int k = 0; k < w; k++) result[k] = gate_ite(a_sign, r_neg[k], r_mag[k]);
        int b_is_zero = gate_not(gate_or_n(b));
        std::vector<int> out(w);
        for (int k = 0; k < w; k++) out[k] = gate_ite(b_is_zero, a[k], result[k]);
        return out;
    }

    std::vector<int> bv_shl(const std::vector<int> &a, const std::vector<int> &b) {
        int w = a.size();
        std::vector<int> result = a;
        int nstages = (int)b.size();
        for (int stage = 0; stage < nstages; stage++) {
            int bit = b[nstages - 1 - stage]; // reversed(b): LSB-first traversal
            int shift_amt = 1 << stage;
            if (shift_amt >= w) {
                std::vector<int> zero_bits(w);
                for (int k = 0; k < w; k++) zero_bits[k] = CONST_FALSE();
                std::vector<int> nr(w);
                for (int k = 0; k < w; k++) nr[k] = gate_ite(bit, zero_bits[k], result[k]);
                result = nr;
                break;
            }
            std::vector<int> shifted(w);
            for (int k = 0; k < w - shift_amt; k++) shifted[k] = result[k + shift_amt];
            for (int k = w - shift_amt; k < w; k++) shifted[k] = CONST_FALSE();
            std::vector<int> nr(w);
            for (int k = 0; k < w; k++) nr[k] = gate_ite(bit, shifted[k], result[k]);
            result = nr;
        }
        return result;
    }
    std::vector<int> bv_lshr(const std::vector<int> &a, const std::vector<int> &b) {
        int w = a.size();
        std::vector<int> result = a;
        int nstages = (int)b.size();
        for (int stage = 0; stage < nstages; stage++) {
            int bit = b[nstages - 1 - stage];
            int shift_amt = 1 << stage;
            if (shift_amt >= w) {
                std::vector<int> zero_bits(w);
                for (int k = 0; k < w; k++) zero_bits[k] = CONST_FALSE();
                std::vector<int> nr(w);
                for (int k = 0; k < w; k++) nr[k] = gate_ite(bit, zero_bits[k], result[k]);
                result = nr;
                break;
            }
            std::vector<int> shifted(w);
            for (int k = 0; k < shift_amt; k++) shifted[k] = CONST_FALSE();
            for (int k = shift_amt; k < w; k++) shifted[k] = result[k - shift_amt];
            std::vector<int> nr(w);
            for (int k = 0; k < w; k++) nr[k] = gate_ite(bit, shifted[k], result[k]);
            result = nr;
        }
        return result;
    }
    std::vector<int> bv_ashr(const std::vector<int> &a, const std::vector<int> &b) {
        int w = a.size();
        int sign = a[0];
        std::vector<int> result = a;
        int nstages = (int)b.size();
        for (int stage = 0; stage < nstages; stage++) {
            int bit = b[nstages - 1 - stage];
            int shift_amt = 1 << stage;
            if (shift_amt >= w) {
                std::vector<int> fill(w);
                for (int k = 0; k < w; k++) fill[k] = sign;
                std::vector<int> nr(w);
                for (int k = 0; k < w; k++) nr[k] = gate_ite(bit, fill[k], result[k]);
                result = nr;
                break;
            }
            std::vector<int> shifted(w);
            for (int k = 0; k < shift_amt; k++) shifted[k] = sign;
            for (int k = shift_amt; k < w; k++) shifted[k] = result[k - shift_amt];
            std::vector<int> nr(w);
            for (int k = 0; k < w; k++) nr[k] = gate_ite(bit, shifted[k], result[k]);
            result = nr;
        }
        return result;
    }
};

// Detect a concrete power-of-2 unsigned value (used for mul/udiv/urem
// strength reduction). 0 is not a power of 2. On success k is its log2.
static bool isPow2Const(unsigned long long v, int &k) {
    if (v == 0 || (v & (v - 1)) != 0) return false;
    k = 0;
    while (v > 1) { v >>= 1; k++; }
    return true;
}

// ─────────────────────────────── IR evaluation ──────────────────────────────

struct Node {
    std::string op;
    std::vector<int> args;      // node ids
    std::vector<long long> params;
    long long value = 0;        // for bvlit
    bool boolValue = false;     // for boollit
    int width = -1;             // -1 = bool
    bool isBool = false;
    std::string name;           // for var
    // Array theory: isArray marks a node of Array sort (var/store/as_const/
    // array-typed ite). idxWidth is the index BV width; elemIsBool/
    // elemWidth describe the element sort the same way width/isBool would
    // for a scalar node.
    bool isArray = false;
    int idxWidth = -1;
    bool elemIsBool = false;
    int elemWidth = -1;
};

struct IRResult {
    // Boolean value cache (node id -> single literal)
    std::unordered_map<int, int> boolCache;
    // BV value cache (node id -> vector of literals, MSB-first)
    std::unordered_map<int, std::vector<int>> bvCache;
    // Array theory: read-over-write encoding state, ported from
    // gansat/ns_bitblaster.py's _Blaster._array_reads. Keyed by the array
    // "root" variable's name (not node id) -- the parser can build several
    // Node objects referring to "the same array" (e.g. two `select`s on a
    // var of the same name), so name is the right identity, matching the
    // Python reference's own reasoning. Value: every (index_bits,
    // result_bits) pair resolved against that array so far, used to add
    // the weak array consistency axiom (idx_i == idx_j -> value_i == value_j)
    // against each new select.
    std::unordered_map<std::string, std::vector<std::pair<std::vector<int>, std::vector<int>>>> arrayReads;
    // SSA variable substitution (var node id -> replacement expr node id).
    // Built once up front from top-level "x = expr" assertions (occurs-
    // checked and statically cycle-checked -- see buildSubstitutions()).
    // Resolved lazily inside blastBV/blastBool: resolving x redirects to
    // blasting the replacement, then the result is ALSO cached under x's
    // OWN node id, so model output (which looks up declared variables by
    // their node id) keeps working transparently for eliminated variables.
    std::unordered_map<int, int> substMap;
    // Defensive dynamic cycle guard, belt-and-suspenders alongside the
    // static cycle check in buildSubstitutions(): if resolving a
    // substitution ever re-enters a node id already being resolved, that's
    // a bug in the static check, not a normal formula -- fail loudly
    // instead of blowing the C++ stack.
    std::unordered_set<int> substInProgress;
};

std::vector<int> blastBV(Blaster &bl, std::vector<Node> &nodes, IRResult &res, int nid);
int blastBool(Blaster &bl, std::vector<Node> &nodes, IRResult &res, int nid);

// select(arrNode, idxNode), returning elemWidth-shaped bits (elemIsBool ->
// a single bit, treated the same as a 1-bit BV throughout).
//
// No dedicated array decision procedure: store/as_const/ite chains are
// peeled by direct term rewriting (read-over-write) rather than
// bit-blasted as arrays in their own right, so a store never itself needs
// an array representation -- only the eventual select does. Bottoms out at
// a genuine array variable (or any other opaque array-valued node), where
// consistency with every previously resolved select against that same
// array is enforced by the weak array axiom: idx_i == idx_j -> value_i ==
// value_j. Ported from gansat/ns_bitblaster.py's _blast_select (see that
// function's own comment for the full reasoning).
//
// Implementation note: every step along a store/ite chain carries the
// *same* idxBits -- only the array node changes as we walk down the chain
// -- so the whole chain is really a tree walk over arrNode alone. That walk
// is done here with an explicit worklist/stack instead of C++-level
// recursion: on real array-heavy formulas a single array can accumulate
// thousands of nested stores (and the store chain itself was already built
// by an iterative let-chain parse for exactly this reason -- see
// smt2ParseTerm), so recursing into it here would just reintroduce the
// same class of stack-depth-proportional-to-chain-length crash the parser
// fix avoided. An iterative walk has no depth ceiling tied to chain length.
std::vector<int> blastSelect(Blaster &bl, std::vector<Node> &nodes, IRResult &res,
                              int arrNode, int idxNode, bool elemIsBool, int elemWidth) {
    int width = elemIsBool ? 1 : elemWidth;
    std::vector<int> idxBits = blastBV(bl, nodes, res, idxNode);

    std::unordered_map<int, std::vector<int>> results;
    std::vector<std::pair<int, bool>> stack;
    stack.push_back({arrNode, false});

    while (!stack.empty()) {
        int nodeId = stack.back().first;
        bool expanded = stack.back().second;
        stack.pop_back();
        if (results.count(nodeId)) continue;
        Node &node = nodes[nodeId];

        if (node.op == "store") {
            int a0 = node.args[0], i0 = node.args[1], v0 = node.args[2];
            if (!expanded) {
                stack.push_back({nodeId, true});
                stack.push_back({a0, false});
                continue;
            }
            std::vector<int> elseBits = results[a0];
            std::vector<int> i0Bits = blastBV(bl, nodes, res, i0);
            int eq = bl.bv_eq(idxBits, i0Bits);
            std::vector<int> thenBits = elemIsBool
                ? std::vector<int>{blastBool(bl, nodes, res, v0)}
                : blastBV(bl, nodes, res, v0);
            std::vector<int> rb(width);
            for (int k = 0; k < width; k++) rb[k] = bl.gate_ite(eq, thenBits[k], elseBits[k]);
            results[nodeId] = rb;
            continue;
        }

        if (node.op == "as_const") {
            std::vector<int> v = elemIsBool
                ? std::vector<int>{blastBool(bl, nodes, res, node.args[0])}
                : blastBV(bl, nodes, res, node.args[0]);
            results[nodeId] = v;
            continue;
        }

        if (node.op == "ite") {
            int tId = node.args[1], eId = node.args[2];
            if (!expanded) {
                stack.push_back({nodeId, true});
                stack.push_back({eId, false});
                stack.push_back({tId, false});
                continue;
            }
            int cond = blastBool(bl, nodes, res, node.args[0]);
            std::vector<int> tBits = results[tId], eBits = results[eId];
            std::vector<int> rb(width);
            for (int k = 0; k < width; k++) rb[k] = bl.gate_ite(cond, tBits[k], eBits[k]);
            results[nodeId] = rb;
            continue;
        }

        // Base case: an opaque array (a var, or any other node we don't
        // peel further). Key by variable name, not node id -- the parser
        // can build multiple Node objects referring to "the same array".
        std::string arrKey = (node.op == "var") ? node.name : ("#anon" + std::to_string(nodeId));
        std::vector<int> resultBits(width);
        for (int k = 0; k < width; k++) resultBits[k] = bl.fresh();

        auto &prior = res.arrayReads[arrKey];
        for (auto &pr : prior) {
            int idxEq = bl.bv_eq(idxBits, pr.first);
            for (int k = 0; k < width; k++) {
                int valEq = bl.gate_not(bl.gate_xor(resultBits[k], pr.second[k]));
                bl.addClauseLits({-idxEq, valEq}); // idx_i==idx_j -> bit_k equal
            }
        }
        prior.push_back({idxBits, resultBits});

        results[nodeId] = resultBits;
    }

    return results[arrNode];
}

// Push an extract down through concat/extract chains instead of always
// blasting the full source node and slicing afterward -- when the
// requested range [hi,lo] (0-indexed from LSB) falls entirely within one
// operand of a concat, or through a nested extract, the OTHER operand
// (or the outer extract's redundant bits) is never blasted at all, not
// just discarded after the fact. Falls back to blast-then-slice for
// anything else (a general expression, or a range spanning both concat
// operands).
static std::vector<int> extractBits(Blaster &bl, std::vector<Node> &nodes, IRResult &res,
                                     int nid, long long hi, long long lo) {
    Node &n = nodes[nid];
    if (n.op == "extract") {
        long long innerLo = n.params[1];
        return extractBits(bl, nodes, res, n.args[0], hi + innerLo, lo + innerLo);
    }
    if (n.op == "concat") {
        long long wB = nodes[n.args[1]].width; // second arg = LSB part
        if (hi < wB) return extractBits(bl, nodes, res, n.args[1], hi, lo);
        if (lo >= wB) return extractBits(bl, nodes, res, n.args[0], hi - wB, lo - wB);
        // Spans both operands -- fall through to the general path below,
        // this case is rare and not worth the extra concat-of-two-partial-
        // extracts bookkeeping for a first version.
    }
    std::vector<int> a = blastBV(bl, nodes, res, nid);
    int w = (int)a.size();
    std::vector<int> out(hi - lo + 1);
    for (long long i = lo; i <= hi; i++) out[hi - i] = a[w - 1 - i];
    return out;
}

// Recognize a node that is a compile-time BV constant, EVEN IF it is not
// a bare bvlit -- real ESBMC output routinely buries a genuine constant
// under its own sign-extension-via-ITE-and-concat encoding (e.g. a
// 32-bit -1 widened to 64 bits for an overflow check becomes
// concat(ite(sign-cond, 0, ones), bvneg(1)), not a plain bvlit). Found
// directly from a real corpus slowdown: bvmul's power-of-2/identity fast
// path was blind to exactly this shape, silently falling through to the
// general O(w^2) multiplier + overflow logic on what is genuinely a
// constant multiply. Handles: bvlit directly; bvneg/bvnot of a constant;
// concat of two constants; extract of a constant; ite with a boollit
// condition (picks the constant branch, recursing -- both branches need
// not themselves be constant, only the live one, matching how the ite
// folding elsewhere in this file already reasons about it). Returns
// nullopt (via the bool out-param) for anything else -- a real variable
// reference or an expression this function does not recognize -- rather
// than guessing.
static bool tryConstEvalBool(std::vector<Node> &nodes, int nid, bool &outVal);
static bool tryConstEval(std::vector<Node> &nodes, int nid, uint64_t &outVal, int &outWidth) {
    Node &n = nodes[nid];
    if (n.op == "bvlit") {
        int w = n.width;
        uint64_t mask = (w >= 64) ? ~0ULL : ((1ULL << w) - 1);
        outVal = (uint64_t)n.value & mask;
        outWidth = w;
        return true;
    }
    if (n.op == "bvneg" || n.op == "bvnot") {
        uint64_t v; int w;
        if (!tryConstEval(nodes, n.args[0], v, w)) return false;
        uint64_t mask = (w >= 64) ? ~0ULL : ((1ULL << w) - 1);
        outVal = (n.op == "bvneg" ? (uint64_t)(-(int64_t)v) : ~v) & mask;
        outWidth = w;
        return true;
    }
    if (n.op == "concat") {
        uint64_t hiV, loV; int hiW, loW;
        if (!tryConstEval(nodes, n.args[0], hiV, hiW)) return false;
        if (!tryConstEval(nodes, n.args[1], loV, loW)) return false;
        if (hiW + loW > 64) return false; // outside what a uint64_t can represent
        outVal = (hiV << loW) | loV;
        outWidth = hiW + loW;
        return true;
    }
    if (n.op == "extract") {
        uint64_t v; int w;
        if (!tryConstEval(nodes, n.args[0], v, w)) return false;
        long long hi = n.params[0], lo = n.params[1];
        int ew = (int)(hi - lo + 1);
        uint64_t emask = (ew >= 64) ? ~0ULL : ((1ULL << ew) - 1);
        outVal = (v >> lo) & emask;
        outWidth = ew;
        return true;
    }
    if (n.op == "ite") {
        bool cond;
        if (!tryConstEvalBool(nodes, n.args[0], cond)) return false;
        int branch = cond ? n.args[1] : n.args[2];
        return tryConstEval(nodes, branch, outVal, outWidth);
    }
    return false;
}

// Companion boolean-sorted constant evaluator, mutually recursive with
// tryConstEval above -- needed because a real sign-extension-via-ite
// condition is routinely a compound expression like "(= (extract 31 31
// x) #b0)" (a sign-bit test), not a bare boollit, so tryConstEval's ite
// case cannot resolve without being able to fold THIS too. Handles:
// boollit directly; = between two BV constant-foldable operands.
static bool tryConstEvalBool(std::vector<Node> &nodes, int nid, bool &outVal) {
    Node &n = nodes[nid];
    if (n.op == "boollit") { outVal = n.boolValue; return true; }
    if (n.op == "=" && n.args.size() == 2 && !nodes[n.args[0]].isArray) {
        uint64_t a, b; int wa, wb;
        if (!tryConstEval(nodes, n.args[0], a, wa)) return false;
        if (!tryConstEval(nodes, n.args[1], b, wb)) return false;
        outVal = (a == b);
        return true;
    }
    return false;
}

std::vector<int> blastBV(Blaster &bl, std::vector<Node> &nodes, IRResult &res, int nid) {
    auto it = res.bvCache.find(nid);
    if (it != res.bvCache.end()) return it->second;
    auto sit = res.substMap.find(nid);
    if (sit != res.substMap.end()) {
        if (!res.substInProgress.insert(nid).second)
            throw std::runtime_error("internal error: SSA substitution cycle at node " + std::to_string(nid));
        std::vector<int> v = blastBV(bl, nodes, res, sit->second);
        res.substInProgress.erase(nid);
        res.bvCache[nid] = v;
        return v;
    }
    Node &n = nodes[nid];
    std::vector<int> out;

    if (n.op == "var") {
        out.resize(n.width);
        for (int i = 0; i < n.width; i++) out[i] = bl.fresh();
    } else if (n.op == "bvlit") {
        out = bl.int_to_bits((uint64_t)n.value, n.width);
    } else if (n.op == "ite") {
        // Constant-condition / identical-branch folding: if the condition
        // is a literal, or both branches are the exact same node (a real,
        // if less common, real-formula pattern -- and one the SSA
        // substitution pass can expose more of, by collapsing what used
        // to be two different node ids into references to the same one),
        // skip gate_ite entirely and return the live branch's bits
        // directly. Any other condition falls through to the general
        // per-bit gate_ite path unchanged.
        if (nodes[n.args[0]].op == "boollit") {
            out = blastBV(bl, nodes, res, nodes[n.args[0]].boolValue ? n.args[1] : n.args[2]);
        } else if (n.args[1] == n.args[2]) {
            out = blastBV(bl, nodes, res, n.args[1]);
        } else {
            int c = blastBool(bl, nodes, res, n.args[0]);
            std::vector<int> t = blastBV(bl, nodes, res, n.args[1]);
            std::vector<int> e = blastBV(bl, nodes, res, n.args[2]);
            out.resize(t.size());
            for (size_t i = 0; i < t.size(); i++) out[i] = bl.gate_ite(c, t[i], e[i]);
        }
    } else if (n.op == "bvadd") {
        out = bl.bv_add(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvsub") {
        out = bl.bv_sub(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvmul") {
        // Strength-reduce x*0 / x*1 / x*2^k (either operand order) before
        // falling back to the general schoolbook multiplier. Each case is
        // an exact rewrite, not an approximation; anything that isn't a
        // concrete bvlit constant on at least one side takes the general
        // bv_mul path unchanged.
        int w = n.width;
        uint64_t mask = (w >= 64) ? ~0ULL : ((1ULL << w) - 1);
        bool handled = false;
        for (int side = 0; side < 2 && !handled; side++) {
            int otherArg = n.args[1 - side];
            uint64_t cv; int cw;
            if (!tryConstEval(nodes, n.args[side], cv, cw)) continue;
            cv &= mask;
            if (cv == 0) {
                out = bl.int_to_bits(0, w);
                handled = true;
            } else if (cv == 1) {
                out = blastBV(bl, nodes, res, otherArg);
                handled = true;
            } else if (cv == mask) {
                // cv == mask (all-ones) is -1 in two-s-complement: x * -1
                // = -x. Not a power of 2 in the unsigned bit pattern, so
                // this needs its own case -- found via a real corpus
                // slowdown (simplifier-mult-fail, x * (-1) with overflow
                // checking): without it, this falls to the general O(w^2)
                // schoolbook multiplier plus overflow-detection logic on
                // top, ~30s+ on a case z3 solves in 0.17s by recognizing
                // the same algebraic identity.
                out = bl.bv_neg(blastBV(bl, nodes, res, otherArg));
                handled = true;
            } else {
                int k;
                if (isPow2Const(cv, k)) {
                    out = bl.bv_shl_const(blastBV(bl, nodes, res, otherArg), k);
                    handled = true;
                } else {
                    out = bl.bv_mul_by_sparse_const(blastBV(bl, nodes, res, otherArg), cv, w);
                    handled = true;
                }
            }
        }
        if (!handled) {
            out = bl.bv_mul(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
        }
    } else if (n.op == "bvudiv") {
        // x udiv 2^k = x >> k, unsigned only. b here is a concrete nonzero
        // constant (power of 2), so the divide-by-zero case of bv_bvudiv
        // can never apply and is safely skipped.
        int w = n.width;
        uint64_t mask = (w >= 64) ? ~0ULL : ((1ULL << w) - 1);
        Node &rhs = nodes[n.args[1]];
        int k;
        if (rhs.op == "bvlit" && isPow2Const((uint64_t)rhs.value & mask, k)) {
            out = bl.bv_lshr_const(blastBV(bl, nodes, res, n.args[0]), k);
        } else {
            out = bl.bv_bvudiv(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
        }
    } else if (n.op == "bvurem") {
        // x urem 2^k = x & (2^k - 1), unsigned only; same zero-divisor
        // reasoning as bvudiv above.
        int w = n.width;
        uint64_t mask = (w >= 64) ? ~0ULL : ((1ULL << w) - 1);
        Node &rhs = nodes[n.args[1]];
        int k;
        if (rhs.op == "bvlit" && isPow2Const((uint64_t)rhs.value & mask, k)) {
            out = bl.bv_urem_pow2_const(blastBV(bl, nodes, res, n.args[0]), k);
        } else {
            out = bl.bv_bvurem(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
        }
    } else if (n.op == "bvsdiv") {
        out = bl.bv_bvsdiv(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvsrem") {
        out = bl.bv_bvsrem(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvneg") {
        out = bl.bv_neg(blastBV(bl, nodes, res, n.args[0]));
    } else if (n.op == "bvnot") {
        out = bl.bv_not(blastBV(bl, nodes, res, n.args[0]));
    } else if (n.op == "bvand" || n.op == "bvor" || n.op == "bvxor") {
        // Trivial constant-operand identities, same pattern/rigor as the
        // bvmul/bvudiv/bvurem strength reductions: x&0=0, x&ones=x,
        // x|0=x, x|ones=ones, x^0=x. Skips gate construction entirely
        // for the folded operand (not just a post-hoc simplification --
        // the non-constant side is still blasted, since its bits may be
        // needed elsewhere in the formula/model, but no AND/OR/XOR gates
        // are built for this node). Any non-constant-operand case falls
        // through unchanged to the general per-bit gate path.
        int w = n.width;
        uint64_t mask = (w >= 64) ? ~0ULL : ((1ULL << w) - 1);
        bool handled = false;
        for (int side = 0; side < 2 && !handled; side++) {
            Node &constNode = nodes[n.args[side]];
            int otherArg = n.args[1 - side];
            if (constNode.op != "bvlit") continue;
            uint64_t cv = (uint64_t)constNode.value & mask;
            if (n.op == "bvand") {
                if (cv == 0) { out = bl.int_to_bits(0, w); handled = true; }
                else if (cv == mask) { out = blastBV(bl, nodes, res, otherArg); handled = true; }
            } else if (n.op == "bvor") {
                if (cv == 0) { out = blastBV(bl, nodes, res, otherArg); handled = true; }
                else if (cv == mask) { out = bl.int_to_bits(mask, w); handled = true; }
            } else { // bvxor
                if (cv == 0) { out = blastBV(bl, nodes, res, otherArg); handled = true; }
            }
        }
        if (!handled) {
            std::vector<int> a = blastBV(bl, nodes, res, n.args[0]);
            std::vector<int> b = blastBV(bl, nodes, res, n.args[1]);
            out = (n.op == "bvand") ? bl.bv_and(a, b) : (n.op == "bvor") ? bl.bv_or(a, b) : bl.bv_xor(a, b);
        }
    } else if (n.op == "bvnand") {
        out = bl.bv_not(bl.bv_and(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1])));
    } else if (n.op == "bvnor") {
        out = bl.bv_not(bl.bv_or(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1])));
    } else if (n.op == "bvxnor") {
        out = bl.bv_not(bl.bv_xor(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1])));
    } else if (n.op == "bvshl") {
        out = bl.bv_shl(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvlshr") {
        out = bl.bv_lshr(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvashr") {
        out = bl.bv_ashr(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "concat") {
        std::vector<int> a = blastBV(bl, nodes, res, n.args[0]);
        std::vector<int> b = blastBV(bl, nodes, res, n.args[1]);
        out = a;
        out.insert(out.end(), b.begin(), b.end());
    } else if (n.op == "extract") {
        // params = [hi, lo], operating on bit positions counted from LSB=0.
        // Delegates to extractBits(), which pushes the extract down through
        // concat/extract chains to avoid blasting bits that would just be
        // discarded -- see that function's own comment.
        out = extractBits(bl, nodes, res, n.args[0], n.params[0], n.params[1]);
    } else if (n.op == "zero_extend") {
        std::vector<int> a = blastBV(bl, nodes, res, n.args[0]);
        long long extra = n.params[0];
        out.resize(a.size() + extra);
        for (long long i = 0; i < extra; i++) out[i] = bl.CONST_FALSE();
        for (size_t i = 0; i < a.size(); i++) out[extra + i] = a[i];
    } else if (n.op == "sign_extend") {
        std::vector<int> a = blastBV(bl, nodes, res, n.args[0]);
        long long extra = n.params[0];
        int sign = a[0];
        out.resize(a.size() + extra);
        for (long long i = 0; i < extra; i++) out[i] = sign;
        for (size_t i = 0; i < a.size(); i++) out[extra + i] = a[i];
    } else if (n.op == "rotate_left" || n.op == "rotate_right") {
        std::vector<int> a = blastBV(bl, nodes, res, n.args[0]);
        int w = (int)a.size();
        long long amt = ((n.params[0] % w) + w) % w;
        out.resize(w);
        if (n.op == "rotate_left") {
            // MSB-first left rotate by amt: new[i] = old[(i+amt) mod w]
            for (int i = 0; i < w; i++) out[i] = a[(i + amt) % w];
        } else {
            for (int i = 0; i < w; i++) out[i] = a[((i - amt) % w + w) % w];
        }
    } else if (n.op == "repeat") {
        std::vector<int> a = blastBV(bl, nodes, res, n.args[0]);
        long long times = n.params[0];
        for (long long t = 0; t < times; t++) out.insert(out.end(), a.begin(), a.end());
    } else if (n.op == "select") {
        out = blastSelect(bl, nodes, res, n.args[0], n.args[1], n.isBool, n.width);
    } else {
        throw std::runtime_error("unsupported BV op in IR: " + n.op);
    }
    res.bvCache[nid] = out;
    return out;
}

int blastBool(Blaster &bl, std::vector<Node> &nodes, IRResult &res, int nid) {
    auto it = res.boolCache.find(nid);
    if (it != res.boolCache.end()) return it->second;
    auto sit = res.substMap.find(nid);
    if (sit != res.substMap.end()) {
        if (!res.substInProgress.insert(nid).second)
            throw std::runtime_error("internal error: SSA substitution cycle at node " + std::to_string(nid));
        int v = blastBool(bl, nodes, res, sit->second);
        res.substInProgress.erase(nid);
        res.boolCache[nid] = v;
        return v;
    }
    Node &n = nodes[nid];
    int out;

    if (n.op == "var") {
        out = bl.fresh();
    } else if (n.op == "boollit") {
        out = n.boolValue ? bl.CONST_TRUE() : bl.CONST_FALSE();
    } else if (n.op == "not") {
        out = bl.gate_not(blastBool(bl, nodes, res, n.args[0]));
    } else if (n.op == "and") {
        std::vector<int> bits;
        for (int a : n.args) bits.push_back(blastBool(bl, nodes, res, a));
        out = bl.gate_and_n(bits);
    } else if (n.op == "or") {
        std::vector<int> bits;
        for (int a : n.args) bits.push_back(blastBool(bl, nodes, res, a));
        out = bl.gate_or_n(bits);
    } else if (n.op == "xor") {
        int acc = blastBool(bl, nodes, res, n.args[0]);
        for (size_t i = 1; i < n.args.size(); i++)
            acc = bl.gate_xor(acc, blastBool(bl, nodes, res, n.args[i]));
        out = acc;
    } else if (n.op == "=>") {
        // right-assoc chain: a1 => (a2 => (... => an))
        int acc = blastBool(bl, nodes, res, n.args.back());
        for (int i = (int)n.args.size() - 2; i >= 0; i--) {
            int ai = blastBool(bl, nodes, res, n.args[i]);
            acc = bl.gate_or(bl.gate_not(ai), acc);
        }
        out = acc;
    } else if (n.op == "ite") {
        // Same constant-condition/identical-branch folding as the BV ite
        // handler above -- see that comment for the reasoning.
        if (nodes[n.args[0]].op == "boollit") {
            out = blastBool(bl, nodes, res, nodes[n.args[0]].boolValue ? n.args[1] : n.args[2]);
        } else if (n.args[1] == n.args[2]) {
            out = blastBool(bl, nodes, res, n.args[1]);
        } else {
            int c = blastBool(bl, nodes, res, n.args[0]);
            int t = blastBool(bl, nodes, res, n.args[1]);
            int e = blastBool(bl, nodes, res, n.args[2]);
            out = bl.gate_ite(c, t, e);
        }
    } else if (n.op == "=") {
        Node &a0 = nodes[n.args[0]];
        if (a0.isArray) {
            // Reflexive case: both operands are literally the same array
            // node (same declared symbol or same let-bound alias resolved
            // to the same node id) -- always true regardless of contents,
            // no extensionality axiom needed. This is sound for any array
            // (including the unbounded-index ESBMC-internal memory-model
            // arrays where full extensional equality is not implementable
            // via index enumeration -- see README "Known limitations").
            // Genuine equality between two *distinct* array terms still
            // requires real extensionality (forall-index) reasoning this
            // bit-blaster does not implement, so it remains a clean error.
            if (n.args[0] == n.args[1]) {
                out = bl.CONST_TRUE();
                res.boolCache[nid] = out;
                return out;
            }
            throw std::runtime_error("smt2 parser: extensional array equality ('=' between two arrays) not supported");
        }
        if (a0.isBool) {
            int a = blastBool(bl, nodes, res, n.args[0]);
            int b = blastBool(bl, nodes, res, n.args[1]);
            out = bl.gate_not(bl.gate_xor(a, b));
        } else {
            std::vector<int> a = blastBV(bl, nodes, res, n.args[0]);
            std::vector<int> b = blastBV(bl, nodes, res, n.args[1]);
            out = bl.bv_eq(a, b);
        }
    } else if (n.op == "distinct") {
        // pairwise distinct
        std::vector<std::vector<int>> vals;
        bool boolArgs = nodes[n.args[0]].isBool;
        std::vector<int> boolVals;
        std::vector<std::vector<int>> bvVals;
        if (boolArgs) {
            for (int a : n.args) boolVals.push_back(blastBool(bl, nodes, res, a));
        } else {
            for (int a : n.args) bvVals.push_back(blastBV(bl, nodes, res, a));
        }
        std::vector<int> neqs;
        for (size_t i = 0; i < n.args.size(); i++)
            for (size_t j = i + 1; j < n.args.size(); j++) {
                int eqlit = boolArgs ? bl.gate_not(bl.gate_xor(boolVals[i], boolVals[j]))
                                      : bl.bv_eq(bvVals[i], bvVals[j]);
                neqs.push_back(bl.gate_not(eqlit));
            }
        out = bl.gate_and_n(neqs);
    } else if (n.op == "bvult") {
        out = bl.bv_ult(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvule") {
        out = bl.bv_ule(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvugt") {
        out = bl.bv_ult(blastBV(bl, nodes, res, n.args[1]), blastBV(bl, nodes, res, n.args[0]));
    } else if (n.op == "bvuge") {
        out = bl.bv_ule(blastBV(bl, nodes, res, n.args[1]), blastBV(bl, nodes, res, n.args[0]));
    } else if (n.op == "bvslt") {
        out = bl.bv_slt(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvsle") {
        out = bl.bv_sle(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvsgt") {
        out = bl.bv_slt(blastBV(bl, nodes, res, n.args[1]), blastBV(bl, nodes, res, n.args[0]));
    } else if (n.op == "bvsge") {
        out = bl.bv_sle(blastBV(bl, nodes, res, n.args[1]), blastBV(bl, nodes, res, n.args[0]));
    } else if (n.op == "select") {
        out = blastSelect(bl, nodes, res, n.args[0], n.args[1], n.isBool, n.width)[0];
    } else {
        throw std::runtime_error("unsupported Bool op in IR: " + n.op);
    }
    res.boolCache[nid] = out;
    return out;
}

// ─────────────────────────────────── main ───────────────────────────────────

struct Declare {
    std::string name;
    int width; // -1 for bool
    bool isBool;
};

struct IR {
    std::vector<Node> nodes;
    std::vector<int> assertions;
    std::vector<Declare> declares;
};

IR loadIR(const std::string &path) {
    JValue root = parseJsonFile(path);
    IR ir;
    const JValue *nodesJ = objGet(root, "nodes");
    ir.nodes.resize(nodesJ->arr.size());
    for (auto &nj : nodesJ->arr) {
        int id = (int)objGet(nj, "id")->asInt();
        Node n;
        n.op = objGet(nj, "op")->asStr();
        if (auto *w = objGet(nj, "width")) if (!w->isNull()) n.width = (int)w->asInt();
        if (auto *ib = objGet(nj, "is_bool")) n.isBool = ib->asBool();
        if (n.op == "var") {
            n.name = objGet(nj, "name")->asStr();
            n.isBool = objGet(nj, "is_bool")->asBool();
        }
        if (n.op == "bvlit") n.value = objGet(nj, "value")->asInt();
        if (n.op == "boollit") n.boolValue = objGet(nj, "value")->asBool();
        if (auto *a = objGet(nj, "args"))
            for (auto &x : a->arr) n.args.push_back((int)x.asInt());
        if (auto *pr = objGet(nj, "params"))
            for (auto &x : pr->arr) n.params.push_back((long long)x.asInt());
        ir.nodes[id] = n;
    }
    for (auto &x : objGet(root, "assertions")->arr) ir.assertions.push_back((int)x.asInt());
    for (auto &dj : objGet(root, "declares")->arr) {
        Declare d;
        d.name = objGet(dj, "name")->asStr();
        d.isBool = objGet(dj, "is_bool")->asBool();
        d.width = d.isBool ? -1 : (int)objGet(dj, "width")->asInt();
        ir.declares.push_back(d);
    }
    return ir;
}

#include "smt2_parser.inc"

static bool hasSuffix(const std::string &s, const std::string &suf) {
    return s.size() >= suf.size() && s.compare(s.size() - suf.size(), suf.size(), suf) == 0;
}

// Renamed from main(): the real entry point below wraps this in a
// try/catch so any unsupported-construct exception (e.g. extensional
// array equality, an unhandled SMT-LIB2 op) exits cleanly with a
// defined non-zero code and a message on stderr, instead of escaping
// uncaught into std::terminate/abort (SIGABRT, possible core dump).
// The wrapper script (neurosym-cpp-solve) already treats any non-zero
// exit as "unknown" and reports it gracefully to ESBMC; this just
// makes that path deterministic and avoids relying on signal handling.
// ── SSA variable substitution (preprocessing, before bit-blasting) ─────────
// Scans top-level assertions for the pattern "x = expr" / "expr = x" where
// x is a bare declared-variable node -- the dominant shape of real
// ESBMC-generated SSA output (sym!N = <expression>, repeated heavily).
// Occurs-checked (x must not appear inside its own expr) and statically
// cycle-checked across the whole substitution set before being trusted --
// see buildSubstitutions(). Deliberately scoped to the direct
// var-equals-expr case only, not linear forms like factor*x+rhs=c (that
// generalization is a distinct, separate piece of work).
//
// Design: rather than physically rewriting the node graph (error-prone on
// a node-id-referencing IR), substitution is applied LAZILY at blast time
// via IRResult::substMap, checked at the top of blastBV/blastBool. This
// has a direct, important side benefit: the resolved result is cached
// under the ORIGINAL variable's node id too, so model-printing (which
// looks up declared variables by node id) keeps reporting correct values
// for eliminated variables with zero special-casing there.

// Bounded DFS: on a huge formula (hundreds of thousands of nodes), an
// unbounded per-candidate walk makes the whole pass O(assertions * nodes)
// -- measured directly to hang for minutes on a real 900K+-variable
// formula. Capping the number of DISTINCT nodes visited bounds each
// check's cost; if the budget runs out before the walk completes, the
// candidate is conservatively treated as "occurs" (or "might cycle",
// same reasoning in substCycles below) and simply not substituted --
// correctness never depends on the budget being large enough, only the
// number of substitutions found does.
static const int OCCURS_CHECK_BUDGET = 300;

static bool nodeOccurs(std::vector<Node> &nodes, int target, int nid,
                        std::unordered_set<int> &visited, int &budget) {
    if (nid == target) return true;
    if (budget-- <= 0) return true; // conservative: assume it occurs
    if (!visited.insert(nid).second) return false;
    Node &n = nodes[nid];
    for (int a : n.args)
        if (nodeOccurs(nodes, target, a, visited, budget)) return true;
    return false;
}

// Static cycle check across the whole tentative substitution set: does
// resolving varNid (transitively, through other substituted variables
// reachable in its replacement expression) ever lead back to varNid?
static bool substCycles(std::vector<Node> &nodes,
                         std::unordered_map<int,int> &tentative,
                         int varNid, int exprNid,
                         std::unordered_set<int> &onStack,
                         std::unordered_set<int> &visited, int &budget) {
    if (budget-- <= 0) return true; // conservative: assume a cycle, drop this candidate
    if (!onStack.insert(exprNid).second) {
        bool cyc = (exprNid == varNid);
        onStack.erase(exprNid);
        return cyc;
    }
    if (exprNid == varNid) { onStack.erase(exprNid); return true; }
    Node &n = nodes[exprNid];
    bool found = false;
    if (n.op == "var") {
        auto it = tentative.find(exprNid);
        if (it != tentative.end())
            found = substCycles(nodes, tentative, varNid, it->second, onStack, visited, budget);
    } else {
        for (int a : n.args) {
            if (substCycles(nodes, tentative, varNid, a, onStack, visited, budget)) { found = true; break; }
        }
    }
    onStack.erase(exprNid);
    return found;
}

static void buildSubstitutions(IR &ir, IRResult &res) {
    std::unordered_map<int,int> tentative; // var node id -> replacement node id
    std::vector<bool> eliminated(ir.assertions.size(), false);

    for (size_t ai = 0; ai < ir.assertions.size(); ai++) {
        int aid = ir.assertions[ai];
        Node &an = ir.nodes[aid];
        if (an.op != "=" || an.args.size() != 2) continue;
        int lhs = an.args[0], rhs = an.args[1];
        int varNid = -1, exprNid = -1;
        if (ir.nodes[lhs].op == "var") { varNid = lhs; exprNid = rhs; }
        else if (ir.nodes[rhs].op == "var") { varNid = rhs; exprNid = lhs; }
        else continue;
        if (varNid == exprNid) continue; // "x = x", not a real definition
        if (tentative.count(varNid)) continue; // first definition wins, per spec

        std::unordered_set<int> visited;
        int budget = OCCURS_CHECK_BUDGET;
        if (nodeOccurs(ir.nodes, varNid, exprNid, visited, budget)) continue; // occurs-check

        tentative[varNid] = exprNid;
        eliminated[ai] = true;
    }

    // Static, whole-set cycle check: verify no chain of tentative
    // substitutions can lead back to its own starting variable. Any
    // substitution found to participate in a cycle is dropped (its
    // assertion reverts to being an ordinary, non-eliminated equality).
    bool changed = true;
    while (changed) {
        changed = false;
        for (auto it = tentative.begin(); it != tentative.end(); ) {
            std::unordered_set<int> onStack, visited;
            int budget = OCCURS_CHECK_BUDGET;
            if (substCycles(ir.nodes, tentative, it->first, it->second, onStack, visited, budget)) {
                // Un-eliminate this one's assertion.
                for (size_t ai = 0; ai < ir.assertions.size(); ai++) {
                    Node &an = ir.nodes[ir.assertions[ai]];
                    if (an.op == "=" && an.args.size() == 2 &&
                        ((an.args[0] == it->first) || (an.args[1] == it->first)) &&
                        eliminated[ai]) {
                        eliminated[ai] = false;
                        break;
                    }
                }
                it = tentative.erase(it);
                changed = true;
            } else {
                ++it;
            }
        }
    }

    res.substMap = tentative;

    std::vector<int> kept;
    for (size_t ai = 0; ai < ir.assertions.size(); ai++)
        if (!eliminated[ai]) kept.push_back(ir.assertions[ai]);
    ir.assertions = kept;
}

static int run_solver(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <ir.json|formula.smt2> [--time] [--smtlib|--json]\n", argv[0]);
        return 2;
    }
    std::string irPath = argv[1];
    bool timing = false;
    bool forceSmt2 = false, forceJson = false;
    bool modelSorts = false; // append " : (_ BitVec N)"/" : Bool" to each model line,
                              // so a pure-shell caller can build ESBMC's (model ...)
                              // block without needing separate declare-fun metadata
                              // (previously supplied by dump_ir.py's JSON IR).
    for (int i = 2; i < argc; i++) {
        std::string a = argv[i];
        if (a == "--time") timing = true;
        else if (a == "--smtlib") forceSmt2 = true;
        else if (a == "--json") forceJson = true;
        else if (a == "--model-sorts") modelSorts = true;
    }
    bool useSmt2 = forceSmt2 || (!forceJson && (hasSuffix(irPath, ".smt2") || hasSuffix(irPath, ".smt")));

    auto t0 = std::chrono::steady_clock::now();

    IR ir = useSmt2 ? parseSmt2File(irPath) : loadIR(irPath);

    SimpSolver S;
    Blaster bl(S);
    IRResult res;
    buildSubstitutions(ir, res);
    // Model printing looks declared variables up in bvCache/boolCache
    // directly, without itself triggering a blast -- so an eliminated
    // variable (substituted away, its defining equality dropped) must be
    // force-blasted here, BEFORE solving, so it lands in the cache under
    // its own node id via the substMap redirect in blastBV/blastBool.
    // This adds no new constraints (blasting alone never asserts
    // anything beyond the Tseitin definition clauses a bit's own gates
    // always need), it only ensures the value is computed and cached for
    // reporting.
    for (auto &kv : res.substMap) {
        int nid = kv.first;
        if (ir.nodes[nid].isBool) blastBool(bl, ir.nodes, res, nid);
        else blastBV(bl, ir.nodes, res, nid);
    }

    // Pre-register var nodes with their variable ids by name so we can
    // report models afterward; and blast declares up front isn't required
    // since blastBV/Bool will allocate lazily via cache keyed on node id
    // referenced from assertions. But some declared vars might not be
    // referenced by any assertion; that's fine, we just won't have a model
    // literal for them (report as unconstrained/0).

    std::vector<int> topLits;
    for (int aid : ir.assertions) {
        int lit;
        if (ir.nodes[aid].isBool || ir.nodes[aid].op == "boollit" ||
            ir.nodes[aid].op == "and" || ir.nodes[aid].op == "or" ||
            ir.nodes[aid].op == "not" || ir.nodes[aid].op == "xor" ||
            ir.nodes[aid].op == "=>" || ir.nodes[aid].op == "=" ||
            ir.nodes[aid].op == "distinct" || ir.nodes[aid].op == "ite" ||
            ir.nodes[aid].op == "bvult" || ir.nodes[aid].op == "bvule" ||
            ir.nodes[aid].op == "bvugt" || ir.nodes[aid].op == "bvuge" ||
            ir.nodes[aid].op == "bvslt" || ir.nodes[aid].op == "bvsle" ||
            ir.nodes[aid].op == "bvsgt" || ir.nodes[aid].op == "bvsge" ||
            ir.nodes[aid].op == "var") {
            lit = blastBool(bl, ir.nodes, res, aid);
        } else {
            throw std::runtime_error("top-level assertion is not boolean-sorted: " + ir.nodes[aid].op);
        }
        topLits.push_back(lit);
    }
    for (int lit : topLits) bl.addClauseLits({lit});

    // Need bv var literal maps for the model report; find var-node ids per
    // declared name (bv var nodes were blasted lazily above only if referenced).
    std::unordered_map<std::string, int> nameToNodeId;
    for (size_t i = 0; i < ir.nodes.size(); i++)
        if (ir.nodes[i].op == "var") nameToNodeId[ir.nodes[i].name] = (int)i;

    auto t1 = std::chrono::steady_clock::now();
    bool sat = S.solve();
    auto t2 = std::chrono::steady_clock::now();

    if (timing) {
        double loadBlastMs = std::chrono::duration<double, std::milli>(t1 - t0).count();
        double solveMs = std::chrono::duration<double, std::milli>(t2 - t1).count();
        fprintf(stderr, "TIMING load_blast_ms=%.3f solve_ms=%.3f total_ms=%.3f\n",
                loadBlastMs, solveMs, loadBlastMs + solveMs);
        fprintf(stderr, "STATS vars=%d xor_cache_hits=%ld xor_cache_misses=%ld xor_vars_avoided=%ld xor_clauses_avoided=%ld and_cache_hits=%ld and_cache_misses=%ld and_vars_avoided=%ld and_clauses_avoided=%ld\n",
                S.nVars(), bl.xorCacheHits, bl.xorCacheMisses, bl.xorVarsAvoided, bl.xorClausesAvoided,
                bl.andCacheHits, bl.andCacheMisses, bl.andVarsAvoided, bl.andClausesAvoided);
    }

    if (!sat) {
        printf("unsat\n");
        return 0;
    }
    printf("sat\n");

    for (auto &d : ir.declares) {
        auto it = nameToNodeId.find(d.name);
        if (it == nameToNodeId.end()) { printf("%s = <unreferenced>\n", d.name.c_str()); continue; }
        int nid = it->second;
        if (d.isBool) {
            auto bit = res.boolCache.find(nid);
            if (bit == res.boolCache.end()) { printf("%s = <unreferenced>\n", d.name.c_str()); continue; }
            int lit = bit->second;
            int var = (lit > 0 ? lit : -lit) - 1;
            lbool v = S.model[var];
            bool val = (lit > 0) ? (v == l_True) : (v == l_False);
            if (modelSorts)
                printf("%s = %s : Bool\n", d.name.c_str(), val ? "true" : "false");
            else
                printf("%s = %s\n", d.name.c_str(), val ? "true" : "false");
        } else {
            auto bit = res.bvCache.find(nid);
            if (bit == res.bvCache.end()) { printf("%s = <unreferenced>\n", d.name.c_str()); continue; }
            const std::vector<int> &bits = bit->second;
            uint64_t val = 0;
            for (size_t i = 0; i < bits.size(); i++) {
                int lit = bits[i];
                int var = (lit > 0 ? lit : -lit) - 1;
                lbool v = S.model[var];
                bool b = (lit > 0) ? (v == l_True) : (v == l_False);
                val = (val << 1) | (b ? 1ULL : 0ULL);
            }
            if (modelSorts)
                printf("%s = %llu : (_ BitVec %d)\n", d.name.c_str(), (unsigned long long)val, d.width);
            else
                printf("%s = %llu\n", d.name.c_str(), (unsigned long long)val);
        }
    }
    return 0;
}

int main(int argc, char **argv) {
    try {
        return run_solver(argc, argv);
    } catch (const std::exception &e) {
        fprintf(stderr, "neurosym bitblast_solver: error: %s\n", e.what());
        return 3;
    } catch (const Minisat::OutOfMemoryException &) {
        // MiniSat's own allocator throws this directly (not derived from
        // std::exception), so it would otherwise fall into the opaque
        // catch(...) below with no diagnostic at all. Found via real RERS
        // corpus testing: several B4 benchmarks bit-blast to a CNF large
        // enough to exhaust an 8GB memory cap mid-search. This is a
        // genuine resource-exhaustion outcome (not a bug to silently
        // paper over), but it deserves a clear, specific message instead
        // of "unknown fatal error" -- same clean-failure standard as the
        // std::exception path just above.
        fprintf(stderr, "neurosym bitblast_solver: out of memory (CNF too large for available memory)\n");
        return 3;
    } catch (...) {
        fprintf(stderr, "neurosym bitblast_solver: unknown fatal error\n");
        return 3;
    }
}
