// Built with -O2 -pthread: two threads stop in worker() at the same frame
// level, each with its own register-allocated ``k``.  Switching threads must
// show the selected thread's ``k`` in the Locals pane.
#include <pthread.h>
#include <stdatomic.h>

static atomic_int arrived;

__attribute__((noinline)) static void *worker(void *arg)
{
    int k = (int)(long)arg * 3 + 1;
    __asm__ volatile("" : "+r"(k));
    atomic_fetch_add(&arrived, 1);
    while (atomic_load(&arrived) < 2) {
        __asm__ volatile("" : "+r"(k));
    }
    __asm__ volatile("nop" : "+r"(k)); // MARK_WORKER
    return (void *)(long)k;
}

int main(void)
{
    pthread_t a, b;
    pthread_create(&a, 0, worker, (void *)10);
    pthread_create(&b, 0, worker, (void *)20);
    pthread_join(a, 0);
    pthread_join(b, 0);
    return 0;
}
