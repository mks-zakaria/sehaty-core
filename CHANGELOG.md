# CHANGELOG

<!-- version list -->

## v1.27.0 (2026-07-15)

### Features

- Add doctor_slug to patient appointment view
  ([#58](https://github.com/mks-zakaria/sehaty-core/pull/58),
  [`23e565b`](https://github.com/mks-zakaria/sehaty-core/commit/23e565bcdf3f7045d6bf673b641051b5b457c018))


## v1.26.0 (2026-07-15)

### Features

- Add appointment reschedule (move to a new free slot)
  ([#56](https://github.com/mks-zakaria/sehaty-core/pull/56),
  [`61f650a`](https://github.com/mks-zakaria/sehaty-core/commit/61f650aa98eea7b094b44c59814b7bc22fd464ef))


## v1.25.0 (2026-07-15)

### Features

- Map booking-race to conflict + persist doctor timezone in profile
  ([#54](https://github.com/mks-zakaria/sehaty-core/pull/54),
  [`a385ea2`](https://github.com/mks-zakaria/sehaty-core/commit/a385ea2b6059e23f661d268bf1b8ec05ae6f49d6))


## v1.24.0 (2026-07-15)

### Features

- Timezone-correct slot generation + availability exceptions
  ([#52](https://github.com/mks-zakaria/sehaty-core/pull/52),
  [`3474374`](https://github.com/mks-zakaria/sehaty-core/commit/347437438901049591973d406ef155dd1689f9b5))


## v1.23.0 (2026-07-15)

### Features

- Patient appointment list with doctor name + patient-scoped prescription detail
  ([#50](https://github.com/mks-zakaria/sehaty-core/pull/50),
  [`d110f7b`](https://github.com/mks-zakaria/sehaty-core/commit/d110f7bf47525262825500db40f3cdf58a2b0238))


## v1.22.0 (2026-07-15)

### Features

- Add doctor appointment-grid list with patient names
  ([#48](https://github.com/mks-zakaria/sehaty-core/pull/48),
  [`6a82f69`](https://github.com/mks-zakaria/sehaty-core/commit/6a82f699dba257e888b7cc16559b8acada118c88))


## v1.21.0 (2026-07-15)

### Features

- Add assistant membership and acting-doctor resolution
  ([#46](https://github.com/mks-zakaria/sehaty-core/pull/46),
  [`0fc98a9`](https://github.com/mks-zakaria/sehaty-core/commit/0fc98a9030e61361befcc3931179bd59ff257ca3))


## v1.20.0 (2026-07-15)

### Features

- Add practice-profile letterheads and freehand prescriptions
  ([#44](https://github.com/mks-zakaria/sehaty-core/pull/44),
  [`2ab3d38`](https://github.com/mks-zakaria/sehaty-core/commit/2ab3d38dd01299ecd632c7361f5db7cf666a1e35))


## v1.19.0 (2026-07-15)

### Features

- Add diagnoses and patient treatment-feedback controllers
  ([#42](https://github.com/mks-zakaria/sehaty-core/pull/42),
  [`13921db`](https://github.com/mks-zakaria/sehaty-core/commit/13921db86f1aba0f91732c1ab42c977d98f492cf))


## v1.18.1 (2026-07-15)

### Bug Fixes

- Compute patient detail no_show_count live to match the list
  ([#40](https://github.com/mks-zakaria/sehaty-core/pull/40),
  [`b7650fd`](https://github.com/mks-zakaria/sehaty-core/commit/b7650fdfc382f68896d2273d667cd7bd78f1a4ad))


## v1.18.0 (2026-07-15)

### Features

- Add patient register (list/detail/aggregates) and auto-link patients on booking
  ([#38](https://github.com/mks-zakaria/sehaty-core/pull/38),
  [`7d8dd70`](https://github.com/mks-zakaria/sehaty-core/commit/7d8dd7046091ff1aceb72ec14dc2d5186cc958b8))


## v1.17.0 (2026-07-14)

### Features

- Persist and return doctor languages ([#36](https://github.com/mks-zakaria/sehaty-core/pull/36),
  [`be2f36c`](https://github.com/mks-zakaria/sehaty-core/commit/be2f36c92e47d57b1b718093923665c29663740f))


## v1.16.0 (2026-07-14)

### Features

- Add admin list_users and list_subscriptions read methods
  ([#34](https://github.com/mks-zakaria/sehaty-core/pull/34),
  [`01e0243`](https://github.com/mks-zakaria/sehaty-core/commit/01e0243ddcd48a408dd519c3839905a720920a7a))


## v1.15.0 (2026-07-14)

### Features

- Add admin-configurable ranking weights and feature flags; search reads them live
  ([#32](https://github.com/mks-zakaria/sehaty-core/pull/32),
  [`6e36a14`](https://github.com/mks-zakaria/sehaty-core/commit/6e36a1415730358761cfd88051844143c2cf16cf))


## v1.14.0 (2026-07-14)

### Features

- Emit in-app notifications on booking, reviews, payments, and referrals
  ([#30](https://github.com/mks-zakaria/sehaty-core/pull/30),
  [`ec58b16`](https://github.com/mks-zakaria/sehaty-core/commit/ec58b16cb0f648d6f8864cf94c494b4c000c36f2))


## v1.13.0 (2026-07-14)

### Features

- Add reporting KPIs, revenue summary, and year-end accounting export
  ([#28](https://github.com/mks-zakaria/sehaty-core/pull/28),
  [`51c8c4d`](https://github.com/mks-zakaria/sehaty-core/commit/51c8c4de1a0eee215fbd801951d6103d75705d7a))


## v1.12.0 (2026-07-14)

### Features

- Add in-app notifications (create, feed, unread count, mark read)
  ([#26](https://github.com/mks-zakaria/sehaty-core/pull/26),
  [`0cc178c`](https://github.com/mks-zakaria/sehaty-core/commit/0cc178c1a317a60652123a4b926a13a5cb638c96))


## v1.11.0 (2026-07-14)

### Features

- Add doctor referral program (code, credit reward on first paid invoice)
  ([#24](https://github.com/mks-zakaria/sehaty-core/pull/24),
  [`5fca900`](https://github.com/mks-zakaria/sehaty-core/commit/5fca9008a2bee0bca3b36e25f120ae505bf53ff8))


## v1.10.0 (2026-07-14)

### Features

- Add cash billing (plans 199/299/499, invoices, receipts, dunning)
  ([#22](https://github.com/mks-zakaria/sehaty-core/pull/22),
  [`7195fec`](https://github.com/mks-zakaria/sehaty-core/commit/7195fec22a645750a15d6b30b3a622115dad1acb))


## v1.9.0 (2026-07-14)

### Features

- Add reviews + reputation (two-way, booking-gated, moderated)
  ([#20](https://github.com/mks-zakaria/sehaty-core/pull/20),
  [`dd6ec9b`](https://github.com/mks-zakaria/sehaty-core/commit/dd6ec9b8060b995db1ca4a63503cc47eab127162))


## v1.8.0 (2026-07-14)

### Features

- Expose doctor id in public doctor view ([#18](https://github.com/mks-zakaria/sehaty-core/pull/18),
  [`3420ecc`](https://github.com/mks-zakaria/sehaty-core/commit/3420eccdfbf1210a1ed0daeb78350558a86cd8ec))


## v1.7.0 (2026-07-14)

### Features

- Add slug-keyed public slots resolver ([#16](https://github.com/mks-zakaria/sehaty-core/pull/16),
  [`e47aec0`](https://github.com/mks-zakaria/sehaty-core/commit/e47aec0cf14fe3a128a73142f8871da3d7561183))


## v1.6.0 (2026-07-14)

### Features

- Add booking core (availability, slots, appointment lifecycle)
  ([#14](https://github.com/mks-zakaria/sehaty-core/pull/14),
  [`e81067f`](https://github.com/mks-zakaria/sehaty-core/commit/e81067fc62ab1ef839fd5f7cb4b9e84da6aa0224))


## v1.5.0 (2026-07-14)

### Features

- Add doctor search with geo + reputation ranking
  ([#12](https://github.com/mks-zakaria/sehaty-core/pull/12),
  [`741c7cc`](https://github.com/mks-zakaria/sehaty-core/commit/741c7ccfa5e91fa20e6bd893428bb36a6c2dd9de))


## v1.4.0 (2026-07-14)

### Features

- Add doctor profile with geolocation + specialties
  ([#10](https://github.com/mks-zakaria/sehaty-core/pull/10),
  [`98f2c3c`](https://github.com/mks-zakaria/sehaty-core/commit/98f2c3c90190ff516516d2a9996181d6a4a312de))


## v1.3.0 (2026-07-14)

### Features

- Add specialties catalogue (list + idempotent seed)
  ([#8](https://github.com/mks-zakaria/sehaty-core/pull/8),
  [`06d11c9`](https://github.com/mks-zakaria/sehaty-core/commit/06d11c9f2e9972816c967f62d9ebb25b15f12763))


## v1.2.0 (2026-07-14)

### Features

- Add accreditation controller (accredit/revoke/list pending)
  ([#6](https://github.com/mks-zakaria/sehaty-core/pull/6),
  [`c4e7b53`](https://github.com/mks-zakaria/sehaty-core/commit/c4e7b5321a9526d33e28a3a745a75de2f1184225))


## v1.1.0 (2026-07-14)

### Features

- Add auth core (password, JWT, refresh rotation, OTP)
  ([#4](https://github.com/mks-zakaria/sehaty-core/pull/4),
  [`4f0d98d`](https://github.com/mks-zakaria/sehaty-core/commit/4f0d98dceb14f001e5d056cd73c27e89b0162408))


## v1.0.0 (2026-07-14)

- Initial Release
