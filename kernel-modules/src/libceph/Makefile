KDIR ?= /usr/src/kernels/4.19.0+1-x86_64
# libceph references crypto_krb5_* exported by krb5.ko — feed its symbols to modpost
KBUILD_EXTRA_SYMBOLS := $(CURDIR)/../krb5/Module.symvers

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) KBUILD_EXTRA_SYMBOLS="$(KBUILD_EXTRA_SYMBOLS)" modules
clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
