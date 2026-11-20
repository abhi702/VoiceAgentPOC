import {
  BadRequestException, ConflictException, Injectable, MessageEvent,
  NotFoundException, OnModuleDestroy,
} from "@nestjs/common";
import { createPool, Pool, PoolConnection, RowDataPacket } from "mysql2/promise";
import { randomUUID } from "node:crypto";
import { interval, map, merge, Observable, Subject } from "rxjs";

interface Slot extends RowDataPacket {
  slot_id: string;
  doctor_name: string;
  slot_date: string;
  slot_time: string;
  status: "AVAILABLE" | "BOOKED";
  booking_key: string | null;
  patient_name: string | null;
  patient_dob: string | null;
}
interface Identity { patient_name: string; patient_dob: string }
interface Mutation extends Identity { call_id: string; idempotency_key: string }
interface Patient extends RowDataPacket { patient_id: string }
interface Prescription extends RowDataPacket {
  prescription_id: string; medication: string; status: string; patient_id: string;
}

@Injectable()
export class BookingsService implements OnModuleDestroy {
  private readonly pool: Pool = createPool({
    host: process.env.MYSQL_HOST ?? "localhost",
    port: Number(process.env.MYSQL_PORT ?? 3306),
    user: process.env.MYSQL_USER ?? "clinic_user",
    password: process.env.MYSQL_PASSWORD,
    database: process.env.MYSQL_DATABASE ?? "gp_clinic",
    connectionLimit: 10, dateStrings: true, timezone: "Z",
  });
  private readonly updates = new Subject<MessageEvent>();

  async onModuleDestroy(): Promise<void> { await this.pool.end(); }
  async health() { await this.pool.query("SELECT 1"); return { status: "ok" }; }

  private clinicNow(): string {
    return new Intl.DateTimeFormat("sv-SE", {
      timeZone: "Europe/London", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).format(new Date());
  }

  private validateIdentity(input: Identity): void {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(input.patient_dob) ||
        input.patient_dob > new Date().toISOString().slice(0, 10)) {
      throw new BadRequestException("Date of birth must be a past ISO date: YYYY-MM-DD");
    }
  }

  async available(date?: string, doctor?: string): Promise<Slot[]> {
    if (date && !/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new BadRequestException("Use YYYY-MM-DD");
    if (doctor && doctor.length > 100) throw new BadRequestException("Doctor name too long");
    const doctorFilter = this.normalizeDoctorFilter(doctor);
    const [rows] = await this.pool.execute<Slot[]>(
      `SELECT slot_id, doctor_name, slot_date, slot_time, status FROM appointment_slots
       WHERE status = 'AVAILABLE' AND TIMESTAMP(slot_date, slot_time) > ?
       AND (? IS NULL OR slot_date = ?)
       AND (? IS NULL OR LOWER(TRIM(REPLACE(REPLACE(LOWER(doctor_name), 'dr. ', ''), 'dr ', ''))) = ?)
       ORDER BY slot_date, slot_time LIMIT 20`,
      [this.clinicNow(), date ?? null, date ?? null, doctorFilter, doctorFilter],
    );
    return rows;
  }

  private normalizeDoctorFilter(doctor?: string): string | null {
    const value = doctor?.trim();
    if (!value || /^(any(?: doctors?)?|all(?: doctors?)?|no preference|either)$/i.test(value)) {
      return null;
    }
    return value.replace(/^(?:dr\.?|doctor)\s+/i, "").trim().toLowerCase() || null;
  }

  async bookings(): Promise<Slot[]> {
    const [rows] = await this.pool.execute<Slot[]>(
      `SELECT slot_id, doctor_name, slot_date, slot_time, status, booked_at
       FROM appointment_slots WHERE status = 'BOOKED' ORDER BY booked_at DESC LIMIT 50`,
    );
    return rows;
  }

  async bookSlot(input: Mutation & { slot_id: string }): Promise<Slot> {
    this.validateIdentity(input);
    const conn = await this.pool.getConnection();
    try {
      await conn.beginTransaction();
      const [rows] = await conn.execute<Slot[]>(
        "SELECT * FROM appointment_slots WHERE slot_id = ? FOR UPDATE NOWAIT", [input.slot_id],
      );
      const slot = rows[0];
      if (!slot) throw new NotFoundException("Slot not found");
      if (slot.booking_key === input.idempotency_key) {
        if (slot.patient_name !== input.patient_name || slot.patient_dob !== input.patient_dob) {
          throw new ConflictException("Idempotency key reused with different details");
        }
        await conn.commit();
        return slot;
      }
      if (slot.status !== "AVAILABLE") throw new ConflictException("Slot is already booked");
      const [future] = await conn.execute<RowDataPacket[]>(
        "SELECT 1 FROM appointment_slots WHERE slot_id = ? AND TIMESTAMP(slot_date, slot_time) > ?",
        [input.slot_id, this.clinicNow()],
      );
      if (!future.length) throw new ConflictException("Slot has expired");
      await conn.execute(
        `UPDATE appointment_slots SET status='BOOKED', patient_name=?, patient_dob=?,
         booked_at=UTC_TIMESTAMP(), booking_key=? WHERE slot_id=?`,
        [input.patient_name, input.patient_dob, input.idempotency_key, input.slot_id],
      );
      await this.audit(input.call_id, "booking_confirmed", { slot_id: input.slot_id }, conn);
      await conn.commit();
      this.updates.next({ data: { type: "booking", slot_id: input.slot_id } });
      return { ...slot, status: "BOOKED", patient_name: input.patient_name, patient_dob: input.patient_dob };
    } catch (error: unknown) {
      await conn.rollback();
      if (typeof error === "object" && error !== null && "code" in error &&
          ["ER_LOCK_NOWAIT", "ER_LOCK_WAIT_TIMEOUT", "ER_DUP_ENTRY"].includes(String(error.code))) {
        throw new ConflictException("Slot or request is being processed; refresh before retrying");
      }
      throw error;
    } finally { conn.release(); }
  }

  private async patient(input: Identity, connection: Pool | PoolConnection = this.pool): Promise<string> {
    this.validateIdentity(input);
    const [rows] = await connection.execute<Patient[]>(
      "SELECT patient_id FROM patients WHERE LOWER(full_name)=LOWER(?) AND date_of_birth=?",
      [input.patient_name.trim(), input.patient_dob],
    );
    if (rows.length !== 1) throw new NotFoundException("No matching synthetic patient");
    return rows[0].patient_id;
  }

  async prescriptionStatus(input: Identity): Promise<Prescription[]> {
    const id = await this.patient(input);
    const [rows] = await this.pool.execute<Prescription[]>(
      "SELECT prescription_id, medication, status, created_at FROM prescriptions WHERE patient_id=? ORDER BY created_at DESC", [id],
    );
    return rows;
  }

  async requestPrescription(input: Mutation & { medication: string }) {
    const conn = await this.pool.getConnection();
    try {
      await conn.beginTransaction();
      const id = await this.patient(input, conn);
      const [existing] = await conn.execute<Prescription[]>(
        "SELECT * FROM prescriptions WHERE request_key=? FOR UPDATE", [input.idempotency_key],
      );
      if (existing.length) {
        if (existing[0].patient_id !== id || existing[0].medication !== input.medication) {
          throw new ConflictException("Idempotency key reused with different details");
        }
        await conn.commit();
        return existing[0];
      }
      const [known] = await conn.execute<Prescription[]>(
        "SELECT prescription_id FROM prescriptions WHERE patient_id=? AND LOWER(medication)=LOWER(?) LIMIT 1",
        [id, input.medication],
      );
      if (!known.length) throw new BadRequestException("Only an existing demo medication can be requested; contact reception");
      const prescription_id = randomUUID();
      await conn.execute(
        "INSERT INTO prescriptions (prescription_id,patient_id,medication,status,request_key) VALUES (?,?,?,'REQUESTED',?)",
        [prescription_id, id, input.medication, input.idempotency_key],
      );
      await this.audit(input.call_id, "prescription_requested", { prescription_id }, conn);
      await conn.commit();
      this.updates.next({ data: { type: "prescription", prescription_id } });
      return { prescription_id, medication: input.medication, status: "REQUESTED" };
    } catch (error: unknown) {
      await conn.rollback();
      throw error;
    } finally { conn.release(); }
  }

  async audit(callId: string, eventType: string, details: object, connection: Pool | PoolConnection = this.pool) {
    await connection.execute(
      "INSERT INTO call_audit_logs (call_id,event_type,details) VALUES (?,?,?)",
      [callId, eventType, JSON.stringify(details)],
    );
    return { recorded: true };
  }

  events(): Observable<MessageEvent> {
    return merge(this.updates, interval(15000).pipe(map(() => ({ data: { type: "heartbeat" } }))));
  }
}
