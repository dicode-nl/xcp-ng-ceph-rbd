KDIR ?= /usr/src/kernels/4.19.0+1-x86_64
KBUILD_EXTRA_SYMBOLS := $(CURDIR)/../libceph/Module.symvers
all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) KBUILD_EXTRA_SYMBOLS="$(KBUILD_EXTRA_SYMBOLS)" modules
clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
