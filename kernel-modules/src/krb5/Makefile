KDIR ?= /usr/src/kernels/4.19.0+1-x86_64

# Production build: no selftests (module_init returns krb5_selftest(); with
# selftests off that stub returns 0, so the module loads cleanly as a library).
# krb5enc excluded: only rfc3962(AES1)/rfc6803(camellia) need it; our target
# enctype rfc8009 (AES256-CTS-HMAC-SHA384-192) uses stock authenc.
obj-m += krb5.o
krb5-y := krb5_kdf.o krb5_api.o rfc3961_simplified.o \
          rfc3962_aes.o rfc6803_camellia.o rfc8009_aes2.o

ccflags-y := -I$(src)/include -include $(src)/compat-4.19.h

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules
clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
