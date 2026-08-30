include $(TOPDIR)/rules.mk

PKG_NAME:=mtkcsi-dump
PKG_VERSION:=2.0.0~git20260830.b8d7b73
PKG_RELEASE:=1

PKG_SOURCE_DATE:=2026-08-30
PKG_SOURCE_PROTO:=git
PKG_SOURCE_URL:=https://github.com/howtion0/MtkCSIdump-csi.git
PKG_SOURCE_VERSION:=b8d7b73fc582795e734086a676a0a18a15980cb8
PKG_SOURCE_SUBMODULES:=skip
PKG_SOURCE_SUBDIR:=$(PKG_NAME)-$(PKG_VERSION)
PKG_SOURCE:=$(PKG_SOURCE_SUBDIR).tar.zst
# A GitHub archive repack is accepted only if it already matches this hash;
# otherwise OpenWrt discards it and raw-git fallback creates the locked,
# owner-0, name-sorted, commit-mtime tar.zst. GitHub's outer gzip is not trusted.
PKG_MIRROR_HASH:=6f02ffbe03a1f5aaa491d1c32babad3595263356ac406f9cc38f64608a835a18
PKG_MAINTAINER:=howtion0
PKG_LICENSE:=Apache-2.0
PKG_LICENSE_FILES:=LICENSE.txt
PKG_INSTALL:=1

include $(INCLUDE_DIR)/package.mk
include $(INCLUDE_DIR)/cmake.mk

define Package/mtkcsi-dump
  SECTION:=utils
  CATEGORY:=Utilities
  TITLE:=Hardened MediaTek CSI capture daemon and UDP v2 exporter
  DEPENDS:=+libnl-tiny +libstdcpp
endef

define Package/mtkcsi-dump/description
 A headless CSI capture service for the hardened MT7915 vendor ABI. The UCI
 service is installed disabled and opens no firewall port by default.
endef

CMAKE_OPTIONS += \
	-DCSI_BUILD_CAPTURE=ON \
	-DBUILD_TESTING=OFF \
	-DCSI_STATIC_LINK=OFF

define Package/mtkcsi-dump/conffiles
/etc/config/mtkcsi
endef

define Package/mtkcsi-dump/install
	$(INSTALL_DIR) $(1)/usr/sbin
	$(INSTALL_BIN) $(PKG_INSTALL_DIR)/usr/sbin/CSIdump $(1)/usr/sbin/mtkcsi-dump
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) ./files/mtkcsi-dump.init $(1)/etc/init.d/mtkcsi-dump
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_CONF) ./files/mtkcsi.config $(1)/etc/config/mtkcsi
	$(INSTALL_DIR) $(1)/usr/share/doc/mtkcsi-dump
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/docs/UDP_V2.md $(1)/usr/share/doc/mtkcsi-dump/UDP_V2.md
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/driver/mt7915-csi/ABI.md $(1)/usr/share/doc/mtkcsi-dump/ABI.md
endef

$(eval $(call BuildPackage,mtkcsi-dump))
