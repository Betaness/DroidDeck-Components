/* Stress test for the thread-id alert protocol of 0003-droiddeck-alert-spin.patch
 * (NtAlertThreadByThreadId / NtWaitForAlertByThreadId on a per-thread futex word).
 *
 * tests/test_alertspin.py cuts the protocol code (the block between "#ifdef USE_FUTEX" and the
 * matching "#endif" that the patch adds to dlls/ntdll/unix/sync.c) out of the patch and builds
 * this file around it, so the test runs the patch's own code on the host:
 *
 *   cc -O2 -pthread -DALERTSPIN_CODE='"block.c"' alertspin.c -o alertspin
 *   WINE_ALERT_SPIN_US=30 ./alertspin 1000000
 *
 * Linux only (real futex system calls). The definitions below stand in for the parts of Wine the
 * block uses; InterlockedExchange and InterlockedCompareExchange are the ones from Wine's winnt.h
 * for gcc and clang, and get_absolute_timeout / update_timeout are copied from sync.c. */

#define _GNU_SOURCE
#include <errno.h>
#include <linux/futex.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

typedef int LONG;
typedef long long LONGLONG;
typedef unsigned long long ULONGLONG;
typedef int BOOL;
typedef unsigned int NTSTATUS;
typedef union { LONGLONG QuadPart; } LARGE_INTEGER;

#define TRUE 1
#define FALSE 0
#define STATUS_ALERTED ((NTSTATUS)0x00000101)
#define STATUS_TIMEOUT ((NTSTATUS)0x00000102)
#define TIMEOUT_INFINITE (((LONGLONG)0x7fffffff << 32) | 0xffffffff)
#define TICKSPERSEC 10000000
#define TICKS_1601_TO_1970 ((ULONGLONG)(369 * 365 + 89) * 86400 * TICKSPERSEC)
#define USE_FUTEX

static inline LONG InterlockedExchange( LONG volatile *dest, LONG val )
{
    return __atomic_exchange_n( dest, val, __ATOMIC_SEQ_CST );
}

static inline LONG InterlockedCompareExchange( LONG volatile *dest, LONG xchg, LONG compare )
{
    return __sync_val_compare_and_swap( dest, compare, xchg );
}

static unsigned long futex_waits, futex_wakes;

static inline int futex_wait( const LONG *addr, int val, struct timespec *timeout )
{
    __atomic_fetch_add( &futex_waits, 1, __ATOMIC_RELAXED );
    return syscall( __NR_futex, addr, FUTEX_WAIT_PRIVATE, val, timeout, 0, 0 );
}

static inline int futex_wake_one( const LONG *addr )
{
    __atomic_fetch_add( &futex_wakes, 1, __ATOMIC_RELAXED );
    return syscall( __NR_futex, addr, FUTEX_WAKE_PRIVATE, 1, NULL, 0, 0 );
}

static inline __attribute__((unused)) ULONGLONG monotonic_counter(void)
{
    struct timespec ts;
    clock_gettime( CLOCK_MONOTONIC, &ts );
    return ts.tv_sec * (ULONGLONG)TICKSPERSEC + ts.tv_nsec / 100;
}

static BOOL alert_simulate_sched_quantum;

#include ALERTSPIN_CODE

static void NtQuerySystemTime( LARGE_INTEGER *time )
{
    struct timespec ts;
    clock_gettime( CLOCK_REALTIME, &ts );
    time->QuadPart = ts.tv_sec * (ULONGLONG)TICKSPERSEC + ts.tv_nsec / 100 + TICKS_1601_TO_1970;
}

static LONGLONG get_absolute_timeout( const LARGE_INTEGER *timeout )
{
    LARGE_INTEGER now;

    if (timeout->QuadPart >= 0) return timeout->QuadPart;
    NtQuerySystemTime( &now );
    return now.QuadPart - timeout->QuadPart;
}

static LONGLONG update_timeout( ULONGLONG end )
{
    LARGE_INTEGER now;
    LONGLONG timeleft;

    NtQuerySystemTime( &now );
    timeleft = end - now.QuadPart;
    if (timeleft < 0) timeleft = 0;
    return timeleft;
}

/* ------------------------------------------------------------------------------------------ */

static int failures;
static const char *current = "start";

#define CHECK(c, ...) do { if (!(c)) { \
    if (__atomic_fetch_add( &failures, 1, __ATOMIC_RELAXED ) < 20) { \
        printf( "FAIL %s line %d: ", current, __LINE__ ); printf( __VA_ARGS__ ); printf( "\n" ); fflush( stdout ); } \
    } } while (0)

static double now_us(void)
{
    struct timespec ts;
    clock_gettime( CLOCK_MONOTONIC, &ts );
    return ts.tv_sec * 1e6 + ts.tv_nsec / 1e3;
}

static unsigned int next_random( unsigned int *state )
{
    unsigned int x = *state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    return *state = x;
}

/* busy for a random time below max_us; now and then a yield instead */
static void jitter( unsigned int *state, unsigned int max_us )
{
    unsigned int r;
    double end;

    if (!max_us) return;
    r = next_random( state );
    if (r % 16 == 0) { sched_yield(); return; }
    if (r % 4 == 0) return;
    end = now_us() + (r >> 8) % max_us;
    while (now_us() < end) ;
}

static LONG word_load( LONG *word )
{
    return __atomic_load_n( word, __ATOMIC_SEQ_CST );
}

static void stats( const char *name, long rounds, unsigned long waits, unsigned long wakes, double start )
{
    double ms = (now_us() - start) / 1000;
    printf( "%-22s %9ld rounds  %8.0f ms  futex_wait %9lu  futex_wake %9lu\n",
            name, rounds, ms, futex_waits - waits, futex_wakes - wakes );
    fflush( stdout );
}

/* 1. ping-pong: A alerts B and waits, B waits and alerts A; plain (non-atomic) data passes along,
 * so ThreadSanitizer sees whether an alert orders it */

static LONG pp_word_a __attribute__((aligned(64)));
static LONG pp_word_b __attribute__((aligned(64)));
static long pp_data, pp_reply;
static long pp_rounds;
static unsigned int pp_jitter;

static void *pingpong_b( void *arg )
{
    unsigned int seed = 0x9e3779b9;
    long i;

    for (i = 0; i < pp_rounds; i++)
    {
        NTSTATUS status = tid_alert_wait( &pp_word_b, NULL );
        CHECK( status == STATUS_ALERTED, "B: wait %#x", status );
        CHECK( pp_data == i, "B: round %ld got data %ld", i, pp_data );
        CHECK( word_load( &pp_word_b ) != TID_ALERT_SLEEPING, "B: word left SLEEPING" );
        pp_reply = i;
        jitter( &seed, pp_jitter );
        tid_alert_wake( &pp_word_a );
    }
    return NULL;
}

static void test_pingpong( long rounds, unsigned int max_jitter_us, const char *name )
{
    unsigned long waits = futex_waits, wakes = futex_wakes;
    unsigned int seed = 0x2545f491;
    double start = now_us();
    pthread_t thread;
    long i;

    current = name;
    pp_rounds = rounds;
    pp_jitter = max_jitter_us;
    pp_data = pp_reply = -1;
    pthread_create( &thread, NULL, pingpong_b, NULL );
    for (i = 0; i < rounds; i++)
    {
        NTSTATUS status;

        pp_data = i;
        jitter( &seed, max_jitter_us );
        tid_alert_wake( &pp_word_b );
        status = tid_alert_wait( &pp_word_a, NULL );
        CHECK( status == STATUS_ALERTED, "A: wait %#x", status );
        CHECK( pp_reply == i, "A: round %ld got reply %ld", i, pp_reply );
        CHECK( word_load( &pp_word_a ) != TID_ALERT_SLEEPING, "A: word left SLEEPING" );
    }
    pthread_join( thread, NULL );
    CHECK( word_load( &pp_word_a ) == TID_ALERT_IDLE && word_load( &pp_word_b ) == TID_ALERT_IDLE,
           "words %d %d at the end", word_load( &pp_word_a ), word_load( &pp_word_b ) );
    stats( name, rounds, waits, wakes, start );
}

/* 2. several threads alert one waiter, each request waits for its acknowledgement (the pattern of
 * RtlWaitOnAddress users: set a value, then wake); a lost alert leaves the waiter asleep for good */

#define MULTI_THREADS 3

static LONG multi_word __attribute__((aligned(64)));
static LONG multi_words[MULTI_THREADS][16] __attribute__((aligned(64)));
static long multi_req[MULTI_THREADS], multi_ack[MULTI_THREADS];
static long multi_rounds;

static void *multi_alerter( void *arg )
{
    int k = (int)(long)arg;
    unsigned int seed = 0x1234567 * (k + 1);
    long r;

    for (r = 1; r <= multi_rounds; r++)
    {
        __atomic_store_n( &multi_req[k], r, __ATOMIC_RELEASE );
        jitter( &seed, r % 3 ? 0 : 20 );
        tid_alert_wake( &multi_word );
        while (__atomic_load_n( &multi_ack[k], __ATOMIC_ACQUIRE ) != r)
        {
            NTSTATUS status = tid_alert_wait( &multi_words[k][0], NULL );
            CHECK( status == STATUS_ALERTED, "alerter %d: wait %#x", k, status );
        }
    }
    return NULL;
}

static void test_multi( long rounds )
{
    unsigned long waits = futex_waits, wakes = futex_wakes;
    double start = now_us();
    pthread_t threads[MULTI_THREADS];
    long seen[MULTI_THREADS] = {0};
    int k, done = 0;

    current = "multi";
    multi_rounds = rounds;
    for (k = 0; k < MULTI_THREADS; k++) pthread_create( &threads[k], NULL, multi_alerter, (void *)(long)k );
    while (done < MULTI_THREADS)
    {
        NTSTATUS status = tid_alert_wait( &multi_word, NULL );
        CHECK( status == STATUS_ALERTED, "waiter: wait %#x", status );
        for (k = 0; k < MULTI_THREADS; k++)
        {
            long r = __atomic_load_n( &multi_req[k], __ATOMIC_ACQUIRE );
            if (r == seen[k]) continue;
            CHECK( r == seen[k] + 1, "alerter %d jumped from %ld to %ld", k, seen[k], r );
            seen[k] = r;
            __atomic_store_n( &multi_ack[k], r, __ATOMIC_RELEASE );
            tid_alert_wake( &multi_words[k][0] );
            if (r == rounds) done++;
        }
    }
    for (k = 0; k < MULTI_THREADS; k++) pthread_join( threads[k], NULL );
    stats( "multi (3 alerters)", rounds * MULTI_THREADS, waits, wakes, start );
}

/* 3. timeouts, one thread (and a helper for a late alert) */

static LONG to_word __attribute__((aligned(64)));

static NTSTATUS timed_wait( LONGLONG quad, double *elapsed_us )
{
    LARGE_INTEGER timeout;
    double start = now_us();
    NTSTATUS status;

    timeout.QuadPart = quad;
    status = tid_alert_wait( &to_word, &timeout );
    if (elapsed_us) *elapsed_us = now_us() - start;
    CHECK( word_load( &to_word ) != TID_ALERT_SLEEPING, "word left SLEEPING after a wait with timeout %lld", quad );
    return status;
}

static void *late_alert( void *arg )
{
    struct timespec ts = { 0, 1000000 };
    nanosleep( &ts, NULL );
    tid_alert_wake( &to_word );
    return NULL;
}

static void test_timeouts(void)
{
    long spin_us = alert_spin_time / 10;
    double elapsed, best;
    LARGE_INTEGER now;
    pthread_t thread;
    NTSTATUS status;
    int i;

    current = "timeouts";

    /* a zero timeout never polls */
    best = 1e9;
    for (i = 0; i < 20; i++)
    {
        status = timed_wait( 0, &elapsed );
        CHECK( status == STATUS_TIMEOUT, "zero timeout: %#x", status );
        if (elapsed < best) best = elapsed;
    }
    CHECK( word_load( &to_word ) == TID_ALERT_IDLE, "word %d after zero timeouts", word_load( &to_word ) );
    if (spin_us >= 500) CHECK( best < spin_us / 2, "zero timeout took %.0f us with %ld us polling", best, spin_us );

    /* a pending alert is consumed by any wait */
    tid_alert_wake( &to_word );
    CHECK( word_load( &to_word ) == TID_ALERT_SIGNALED, "word %d after an alert", word_load( &to_word ) );
    status = timed_wait( 0, NULL );
    CHECK( status == STATUS_ALERTED, "pending alert, zero timeout: %#x", status );
    status = timed_wait( 0, NULL );
    CHECK( status == STATUS_TIMEOUT, "consumed alert, zero timeout: %#x", status );
    tid_alert_wake( &to_word );
    tid_alert_wake( &to_word );
    status = timed_wait( TIMEOUT_INFINITE, NULL );
    CHECK( status == STATUS_ALERTED, "pending alert, infinite timeout: %#x", status );
    status = timed_wait( -10000, NULL );
    CHECK( status == STATUS_TIMEOUT, "two alerts count once: %#x", status );

    /* relative timeouts run their full length, polling included */
    best = 1e9;
    for (i = 0; i < 5; i++)
    {
        status = timed_wait( -20000, &elapsed );
        CHECK( status == STATUS_TIMEOUT, "2 ms timeout: %#x", status );
        CHECK( elapsed >= 1990, "2 ms timeout returned after %.0f us", elapsed );
        if (elapsed < best) best = elapsed;
    }
    if (spin_us >= 500) CHECK( best < 2000 + spin_us * 0.8, "2 ms timeout took %.0f us with %ld us polling", best, spin_us );
    CHECK( word_load( &to_word ) == TID_ALERT_IDLE, "word %d after timeouts", word_load( &to_word ) );

    /* a relative timeout shorter than the polling time does not poll */
    best = 1e9;
    for (i = 0; i < 10; i++)
    {
        status = timed_wait( -2000, &elapsed );
        CHECK( status == STATUS_TIMEOUT, "200 us timeout: %#x", status );
        CHECK( elapsed >= 195, "200 us timeout returned after %.0f us", elapsed );
        if (elapsed < best) best = elapsed;
    }
    if (spin_us >= 500) CHECK( best < spin_us * 0.7, "200 us timeout took %.0f us with %ld us polling", best, spin_us );

    /* absolute timeouts */
    NtQuerySystemTime( &now );
    status = timed_wait( now.QuadPart - 10000, &elapsed );
    CHECK( status == STATUS_TIMEOUT, "absolute timeout in the past: %#x", status );
    NtQuerySystemTime( &now );
    status = timed_wait( now.QuadPart + 20000, &elapsed );
    CHECK( status == STATUS_TIMEOUT, "absolute timeout: %#x", status );
    CHECK( elapsed >= 1900, "absolute 2 ms timeout returned after %.0f us", elapsed );

    /* an alert after a timeout stays pending for the next wait */
    status = timed_wait( -1000, NULL );
    CHECK( status == STATUS_TIMEOUT, "100 us timeout: %#x", status );
    tid_alert_wake( &to_word );
    status = timed_wait( 0, NULL );
    CHECK( status == STATUS_ALERTED, "alert after a timeout: %#x", status );

    /* an alert ends a long timed wait early */
    pthread_create( &thread, NULL, late_alert, NULL );
    status = timed_wait( -10 * (LONGLONG)TICKSPERSEC, &elapsed );
    pthread_join( thread, NULL );
    CHECK( status == STATUS_ALERTED, "alert during a 10 s timeout: %#x", status );
    CHECK( elapsed < 500000, "alert during a 10 s timeout took %.0f us", elapsed );
    CHECK( word_load( &to_word ) == TID_ALERT_IDLE, "word %d at the end", word_load( &to_word ) );
    printf( "%-22s ok\n", "timeouts" );
}

/* 4. alerts racing timeouts: the waiter waits with short random timeouts until it gets the round's
 * single alert (one round in four with a 2 s timeout, where a timeout means a lost wake);
 * exactly one ALERTED per alert, and the word is IDLE right after it */

static LONG race_word __attribute__((aligned(64)));
static LONG race_ack_word __attribute__((aligned(64)));
static long race_sent, race_ack;
static long race_rounds;

static void *race_alerter( void *arg )
{
    unsigned int seed = 0xdeadbeef;
    long r;

    for (r = 1; r <= race_rounds; r++)
    {
        jitter( &seed, 60 );
        __atomic_store_n( &race_sent, r, __ATOMIC_RELEASE );
        tid_alert_wake( &race_word );
        while (__atomic_load_n( &race_ack, __ATOMIC_ACQUIRE ) != r)
        {
            NTSTATUS status = tid_alert_wait( &race_ack_word, NULL );
            CHECK( status == STATUS_ALERTED, "alerter: wait %#x", status );
        }
    }
    return NULL;
}

static void test_race( long rounds )
{
    unsigned long waits = futex_waits, wakes = futex_wakes;
    double start = now_us();
    unsigned int seed = 0xc0ffee;
    long r, timeouts = 0;
    pthread_t thread;

    current = "race";
    race_rounds = rounds;
    pthread_create( &thread, NULL, race_alerter, NULL );
    for (r = 1; r <= rounds; r++)
    {
        BOOL strict = r % 4 == 0;
        NTSTATUS status;
        long sent;

        for (;;)
        {
            LARGE_INTEGER timeout;

            timeout.QuadPart = strict ? -2 * (LONGLONG)TICKSPERSEC : -(LONGLONG)(20 + next_random( &seed ) % 800);
            status = tid_alert_wait( &race_word, &timeout );
            if (status != STATUS_TIMEOUT) break;
            CHECK( word_load( &race_word ) != TID_ALERT_SLEEPING, "word left SLEEPING after a timeout" );
            CHECK( !strict, "round %ld: 2 s timeout although the alert was due within 60 us (lost wake)", r );
            timeouts++;
        }
        CHECK( status == STATUS_ALERTED, "round %ld: wait %#x", r, status );
        sent = __atomic_load_n( &race_sent, __ATOMIC_ACQUIRE );
        CHECK( sent == r, "round %ld: ALERTED before the alert (sent %ld)", r, sent );
        CHECK( word_load( &race_word ) == TID_ALERT_IDLE, "round %ld: word %d after the alert", r, word_load( &race_word ) );
        __atomic_store_n( &race_ack, r, __ATOMIC_RELEASE );
        tid_alert_wake( &race_ack_word );
    }
    pthread_join( thread, NULL );
    stats( "race (timeouts)", rounds, waits, wakes, start );
    printf( "%-22s %9ld timeouts before the alert\n", "", timeouts );
}

static void hang( int sig )
{
    static const char msg[] = "FAIL: no progress before the deadline (a lost wakeup?)\n";
    if (write( 1, msg, sizeof(msg) - 1 )) _exit( 3 );
    _exit( 3 );
}

int main( int argc, char **argv )
{
    long rounds = argc > 1 ? atol( argv[1] ) : 100000;
    unsigned int deadline = argc > 2 ? atoi( argv[2] ) : 600;

    setvbuf( stdout, NULL, _IOLBF, 0 );
    signal( SIGALRM, hang );
    alarm( deadline );
    printf( "polling %llu clock ticks (%lld us), %ld rounds\n", alert_spin_ticks, alert_spin_time / 10, rounds );

    test_timeouts();
    test_pingpong( rounds, 0, "pingpong" );
    test_pingpong( rounds / 4 + 1, 50, "pingpong (jitter)" );
    test_multi( rounds / 4 + 1 );
    test_race( rounds / 10 + 1 );

    printf( failures ? "FAIL %d\n" : "PASS\n", failures );
    return failures ? 1 : 0;
}
