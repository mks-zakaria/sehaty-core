# CHANGELOG

<!-- version list -->

## v1.44.0 (2026-07-19)

### Features

- **core**: Pharmacy POS controllers — products + sales
  ([#127](https://github.com/mks-zakaria/sehaty-core/pull/127),
  [`6f1dcd5`](https://github.com/mks-zakaria/sehaty-core/commit/6f1dcd51ab556b2e83cefccec80575fb68a6a6af))


## v1.43.0 (2026-07-19)

### Features

- **core**: Medication catalogue search
  ([#125](https://github.com/mks-zakaria/sehaty-core/pull/125),
  [`db24112`](https://github.com/mks-zakaria/sehaty-core/commit/db2411218f140cd5114c253c775cb5b396729d09))


## v1.42.0 (2026-07-19)

### Features

- **core**: Specialty darija + directory name search
  ([#123](https://github.com/mks-zakaria/sehaty-core/pull/123),
  [`7e95aee`](https://github.com/mks-zakaria/sehaty-core/commit/7e95aeecc47490d6a05f496b807abdb4f6d04441))


## v1.41.0 (2026-07-19)

### Features

- **core**: Pharmacy stock management ([#121](https://github.com/mks-zakaria/sehaty-core/pull/121),
  [`59be798`](https://github.com/mks-zakaria/sehaty-core/commit/59be798e22b64c79eca0ab704e3becf68ef235df))


## v1.40.0 (2026-07-19)

### Features

- **core**: Pharmacy dispensing controller
  ([#119](https://github.com/mks-zakaria/sehaty-core/pull/119),
  [`7e4db61`](https://github.com/mks-zakaria/sehaty-core/commit/7e4db6122ce20055e98792264f50bd4a8a5c17e7))


## v1.39.0 (2026-07-19)

### Features

- **core**: Add Prescription Items sheet to the doctor export
  ([#117](https://github.com/mks-zakaria/sehaty-core/pull/117),
  [`bef801f`](https://github.com/mks-zakaria/sehaty-core/commit/bef801fcf4758a4170f771cb2d7c2c5efc3b3ae9))


## v1.38.0 (2026-07-19)

### Features

- **core**: Add Reviews and Billing sheets to the doctor export
  ([#115](https://github.com/mks-zakaria/sehaty-core/pull/115),
  [`2b4bc54`](https://github.com/mks-zakaria/sehaty-core/commit/2b4bc5424c13cb1a0bea3cd965862e49bd102a87))


## v1.37.0 (2026-07-19)

### Features

- **core**: Doctor data export controller
  ([#113](https://github.com/mks-zakaria/sehaty-core/pull/113),
  [`64eeea7`](https://github.com/mks-zakaria/sehaty-core/commit/64eeea7465205a6e2a1b927143dd8c025266eda1))


## v1.36.0 (2026-07-19)

### Features

- **core**: Active session lookup for a secretary's doctor
  ([#111](https://github.com/mks-zakaria/sehaty-core/pull/111),
  [`887d5ce`](https://github.com/mks-zakaria/sehaty-core/commit/887d5ce6b8886b5f0d78ea8ea075f2667105cb66))


## v1.35.0 (2026-07-19)

### Code Style

- **core**: Sort imports in appointments controller
  ([#107](https://github.com/mks-zakaria/sehaty-core/pull/107),
  [`efd3bba`](https://github.com/mks-zakaria/sehaty-core/commit/efd3bbab9264a7cfaccf6870213a58564ff2b27f))

### Features

- **core**: Cabinet controller + consultation flow
  ([#109](https://github.com/mks-zakaria/sehaty-core/pull/109),
  [`2c69d77`](https://github.com/mks-zakaria/sehaty-core/commit/2c69d7796829d7b09bf5b8b5965188edf37df888))

### Refactoring

- **core**: Add DomainModel base for domain projections
  ([#74](https://github.com/mks-zakaria/sehaty-core/pull/74),
  [`4033a50`](https://github.com/mks-zakaria/sehaty-core/commit/4033a50c4608698f7c2978cd03b2798a49892319))

- **core**: Promote analytics projections to DomainModel
  ([#76](https://github.com/mks-zakaria/sehaty-core/pull/76),
  [`da8cc1a`](https://github.com/mks-zakaria/sehaty-core/commit/da8cc1a33fee0a6cba52557036ff893e808e95d9))

- **core**: Promote appointment projections to DomainModel (adds AppointmentRow)
  ([#78](https://github.com/mks-zakaria/sehaty-core/pull/78),
  [`7c776f6`](https://github.com/mks-zakaria/sehaty-core/commit/7c776f63d244f7bde8b0a3ae6520f40400ef3da9))

- **core**: Promote availability-exception & prescription-template projections to DomainModel
  ([#88](https://github.com/mks-zakaria/sehaty-core/pull/88),
  [`45903c3`](https://github.com/mks-zakaria/sehaty-core/commit/45903c371ec2a54a7ebc73858f3baa76ac103b2f))

- **core**: Promote billing & reporting projections to DomainModel
  ([#84](https://github.com/mks-zakaria/sehaty-core/pull/84),
  [`8fa3a4b`](https://github.com/mks-zakaria/sehaty-core/commit/8fa3a4b2166cd7299756b8884d6852f51f517c3d))

- **core**: Promote clinical projections to DomainModel
  ([#80](https://github.com/mks-zakaria/sehaty-core/pull/80),
  [`c541c96`](https://github.com/mks-zakaria/sehaty-core/commit/c541c96fb6b7cd7b0b43e8bea1124aef8332684f))

- **core**: Promote config & dashboard projections to DomainModel
  ([#90](https://github.com/mks-zakaria/sehaty-core/pull/90),
  [`44b70b6`](https://github.com/mks-zakaria/sehaty-core/commit/44b70b6a6011da83132ef6176050744263bd47a7))

- **core**: Promote doctor/directory/admin projections to DomainModel
  ([#92](https://github.com/mks-zakaria/sehaty-core/pull/92),
  [`02107d8`](https://github.com/mks-zakaria/sehaty-core/commit/02107d8ea56b19a29ab16c589526a8da296cf1ec))

- **core**: Promote messaging projections to DomainModel
  ([#82](https://github.com/mks-zakaria/sehaty-core/pull/82),
  [`8215ba2`](https://github.com/mks-zakaria/sehaty-core/commit/8215ba237d31b73df86b4cd13400d9afb3e77208))

- **core**: Promote patient & assistant projections to DomainModel
  ([#86](https://github.com/mks-zakaria/sehaty-core/pull/86),
  [`71fe1f3`](https://github.com/mks-zakaria/sehaty-core/commit/71fe1f3a2b4a9da27f48a021a78f3ca3daade467))

- **core**: Return AvailabilityRow projection instead of Availability ORM
  ([#103](https://github.com/mks-zakaria/sehaty-core/pull/103),
  [`f9ae2a1`](https://github.com/mks-zakaria/sehaty-core/commit/f9ae2a1e3ad06248145a1c36258d76505aacad44))

- **core**: Return MeView projection from register_doctor
  ([#106](https://github.com/mks-zakaria/sehaty-core/pull/106),
  [`2ef71ea`](https://github.com/mks-zakaria/sehaty-core/commit/2ef71eaed59c15ba0ef9a166cdddfa434ea0da23))

- **core**: Return NotificationRow projection instead of Notification ORM
  ([#104](https://github.com/mks-zakaria/sehaty-core/pull/104),
  [`0800d30`](https://github.com/mks-zakaria/sehaty-core/commit/0800d305fca83e3f36cdbbd3da95b6eb2b4370b6))

- **core**: Return Plan/Subscription/Payment projections instead of ORM
  ([#102](https://github.com/mks-zakaria/sehaty-core/pull/102),
  [`cf4efe3`](https://github.com/mks-zakaria/sehaty-core/commit/cf4efe3dc460b988a956cfda1c9f9f8b1123d0c8))

- **core**: Return ReferralRow projection instead of Referral ORM
  ([#105](https://github.com/mks-zakaria/sehaty-core/pull/105),
  [`7a155bf`](https://github.com/mks-zakaria/sehaty-core/commit/7a155bf6becd3a42c3850f70bdeab264e7121460))

- **core**: Return ReviewRow projection instead of Review ORM
  ([#101](https://github.com/mks-zakaria/sehaty-core/pull/101),
  [`4b0bf4d`](https://github.com/mks-zakaria/sehaty-core/commit/4b0bf4d3f1aabe14735cc43a70e798939a1472c1))


## v1.34.0 (2026-07-16)

### Features

- Add messaging controller ([#72](https://github.com/mks-zakaria/sehaty-core/pull/72),
  [`eeb4325`](https://github.com/mks-zakaria/sehaty-core/commit/eeb4325ca6c2e5a4922cafbcf2b7468029177fdb))


## v1.33.0 (2026-07-16)

### Features

- Add doctor analytics (monthly appointments, no-show rate, estimated revenue, review trend)
  ([#70](https://github.com/mks-zakaria/sehaty-core/pull/70),
  [`16dd002`](https://github.com/mks-zakaria/sehaty-core/commit/16dd002a0d1364ed200d15c3af9a59a485bee91b))


## v1.32.0 (2026-07-16)

### Features

- Add doctor directory (browse by specialty and rating)
  ([#68](https://github.com/mks-zakaria/sehaty-core/pull/68),
  [`6512ae3`](https://github.com/mks-zakaria/sehaty-core/commit/6512ae3b69e74e75a24dd6822c512d5326911f38))


## v1.31.0 (2026-07-15)

### Features

- Include doctor slug in assistant's doctor list
  ([#66](https://github.com/mks-zakaria/sehaty-core/pull/66),
  [`b8eb4ee`](https://github.com/mks-zakaria/sehaty-core/commit/b8eb4eeda826f7563e3527040d24b7f370ea436a))


## v1.30.0 (2026-07-15)

### Features

- Add prescription template CRUD controller
  ([#64](https://github.com/mks-zakaria/sehaty-core/pull/64),
  [`a94a9f6`](https://github.com/mks-zakaria/sehaty-core/commit/a94a9f667fce05fd9d52e0e1428f09b4d46d24b0))


## v1.29.0 (2026-07-15)

### Features

- Add doctor dashboard stats (today, to-confirm, upcoming, patients, next appointment)
  ([#62](https://github.com/mks-zakaria/sehaty-core/pull/62),
  [`6d3c8e5`](https://github.com/mks-zakaria/sehaty-core/commit/6d3c8e53be1310bf63b90f060087d7252a0ea1d9))


## v1.28.0 (2026-07-15)

### Features

- Add run_reminders (one-time patient reminder for upcoming confirmed appointments)
  ([#60](https://github.com/mks-zakaria/sehaty-core/pull/60),
  [`92dd636`](https://github.com/mks-zakaria/sehaty-core/commit/92dd6362226bef915d26d5f506a7a35cc19b0422))


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
