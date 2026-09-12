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
        int y = fresh();
        addClauseLits({-y, a});
        addClauseLits({-y, b});
        addClauseLits({y, -a, -b});
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
        int y = fresh();
        addClauseLits({-y, -a, -b});
        addClauseLits({-y, a, b});
        addClauseLits({y, -a, b});
        addClauseLits({y, a, -b});
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
            std::vector<int> shifted(w);
            for (int k = 0; k < shift; k++) shifted[k] = CONST_FALSE();
            for (int k = shift; k < w; k++) shifted[k] = a[k - shift];
            std::vector<int> masked(w);
            for (int j = 0; j < w; j++) masked[j] = gate_and(shifted[j], b[i]);
            result = bv_add(result, masked);
        }
        return result;
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
};

struct IRResult {
    // Boolean value cache (node id -> single literal)
    std::unordered_map<int, int> boolCache;
    // BV value cache (node id -> vector of literals, MSB-first)
    std::unordered_map<int, std::vector<int>> bvCache;
};

std::vector<int> blastBV(Blaster &bl, std::vector<Node> &nodes, IRResult &res, int nid);
int blastBool(Blaster &bl, std::vector<Node> &nodes, IRResult &res, int nid);

std::vector<int> blastBV(Blaster &bl, std::vector<Node> &nodes, IRResult &res, int nid) {
    auto it = res.bvCache.find(nid);
    if (it != res.bvCache.end()) return it->second;
    Node &n = nodes[nid];
    std::vector<int> out;

    if (n.op == "var") {
        out.resize(n.width);
        for (int i = 0; i < n.width; i++) out[i] = bl.fresh();
    } else if (n.op == "bvlit") {
        out = bl.int_to_bits((uint64_t)n.value, n.width);
    } else if (n.op == "ite") {
        int c = blastBool(bl, nodes, res, n.args[0]);
        std::vector<int> t = blastBV(bl, nodes, res, n.args[1]);
        std::vector<int> e = blastBV(bl, nodes, res, n.args[2]);
        out.resize(t.size());
        for (size_t i = 0; i < t.size(); i++) out[i] = bl.gate_ite(c, t[i], e[i]);
    } else if (n.op == "bvadd") {
        out = bl.bv_add(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvsub") {
        out = bl.bv_sub(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvmul") {
        out = bl.bv_mul(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvudiv") {
        out = bl.bv_bvudiv(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvurem") {
        out = bl.bv_bvurem(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvsdiv") {
        out = bl.bv_bvsdiv(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvsrem") {
        out = bl.bv_bvsrem(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvneg") {
        out = bl.bv_neg(blastBV(bl, nodes, res, n.args[0]));
    } else if (n.op == "bvnot") {
        out = bl.bv_not(blastBV(bl, nodes, res, n.args[0]));
    } else if (n.op == "bvand") {
        out = bl.bv_and(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvor") {
        out = bl.bv_or(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
    } else if (n.op == "bvxor") {
        out = bl.bv_xor(blastBV(bl, nodes, res, n.args[0]), blastBV(bl, nodes, res, n.args[1]));
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
        // params = [hi, lo], operating on bit positions counted from LSB=0
        std::vector<int> a = blastBV(bl, nodes, res, n.args[0]);
        int w = (int)a.size();
        long long hi = n.params[0], lo = n.params[1];
        // a is MSB-first; index from MSB-first: LSB index i (0-based from
        // right) corresponds to a[w-1-i]
        out.resize(hi - lo + 1);
        for (long long i = lo; i <= hi; i++) out[hi - i] = a[w - 1 - i];
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
    } else {
        throw std::runtime_error("unsupported BV op in IR: " + n.op);
    }
    res.bvCache[nid] = out;
    return out;
}

int blastBool(Blaster &bl, std::vector<Node> &nodes, IRResult &res, int nid) {
    auto it = res.boolCache.find(nid);
    if (it != res.boolCache.end()) return it->second;
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
        int c = blastBool(bl, nodes, res, n.args[0]);
        int t = blastBool(bl, nodes, res, n.args[1]);
        int e = blastBool(bl, nodes, res, n.args[2]);
        out = bl.gate_ite(c, t, e);
    } else if (n.op == "=") {
        Node &a0 = nodes[n.args[0]];
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

int main(int argc, char **argv) {
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
