#!/usr/bin/env ruby
# frozen_string_literal: true

# Lightroom Classic Backups retention script (Ruby port).
#
# Keeps:
# - Latest backup per day (collapsing multiple runs per day)
# - Last N daily backups (most recent backup-days)
# - Last N weekly backups (latest backup in a week among older daily backups; may skip weeks)
# - Last N monthly backups (latest backup in a month among older daily backups; may skip months)
#
# Default: dry-run (prints what it would delete).
# Use --apply to actually delete.
#
# Typical Lightroom backup structure:
# Backups/
#   YYYY-MM-DD HHMM/
#     <catalog>.lrcat.zip
#
# Usage:
#   ruby lrc-backup-retention.rb [options] BACKUPS_ROOT
#
# Options:
#   --keep-days N      Number of daily backups to keep (default: 7)
#   --keep-weeks N     Number of weekly backups to keep (default: 4)
#   --keep-months N    Number of monthly backups to keep (default: 3)
#   --apply            Actually delete; otherwise dry-run
#   -h, --help         Show help
#
# Examples:
#   # Preview what would be deleted, using default retention tiers
#   ruby lrc-backup-retention.rb "~/Pictures/Lightroom/Backups"
#
#   # Keep 14 daily, 8 weekly, 6 monthly backups, and actually delete the rest
#   ruby lrc-backup-retention.rb --keep-days 14 --keep-weeks 8 --keep-months 6 --apply \
#     "~/Pictures/Lightroom/Backups"

require 'date'
require 'fileutils'
require 'optparse'
require 'pathname'
require 'set'
require 'time'

# A single Lightroom backup directory.
#
# @!attribute folder
#   @return [Pathname] the backup directory (a direct child of the Backups root)
# @!attribute dt
#   @return [Time] timestamp used for retention grouping (parsed from the folder
#     name, or falling back to the folder's mtime)
# @!attribute zip_files
#   @return [Array<Pathname>] the catalog .zip file(s) found inside the folder
Backup = Struct.new(:folder, :dt, :zip_files)

DEFAULT_BACKUPS_ROOT = Pathname.new('~/Pictures/Lightroom/Backups').expand_path

# Recognized Lightroom Classic backup folder name formats, paired with the
# +Time.strptime+ format string used to parse each one.
# @return [Array<Array(Regexp, String)>]
FOLDER_DT_PATTERNS = [
  [/^\d{4}-\d{2}-\d{2} \d{4}$/, '%Y-%m-%d %H%M'],
  [/^\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}$/, '%Y-%m-%d %H-%M-%S'],
  [/^\d{4}-\d{2}-\d{2}_\d{4}$/, '%Y-%m-%d_%H%M']
].freeze

# Determine the best-effort timestamp for a backup folder.
#
# 1) Parse folder name like 'YYYY-MM-DD HHMM' (or other recognized formats)
# 2) Else fall back to the folder's mtime
#
# @param folder [Pathname] backup directory to inspect
# @return [Time] the resolved timestamp
def parse_backup_datetime(folder)
  name = folder.basename.to_s
  FOLDER_DT_PATTERNS.each do |rx, fmt|
    next unless rx.match?(name)

    begin
      return Time.strptime(name, fmt)
    rescue ArgumentError
      next
    end
  end
  folder.mtime
end

# Scan a Lightroom Backups root directory and build the list of backups.
#
# Only child directories that contain at least one +*.zip+ file are
# considered backups; empty or unrelated directories are skipped.
#
# @param backups_root [Pathname] path to the Lightroom 'Backups' folder
# @return [Array<Backup>] backups sorted newest-first
# @raise [Errno::ENOENT] if +backups_root+ does not exist
def find_backups(backups_root)
  raise Errno::ENOENT, "Backups folder not found: #{backups_root}" unless backups_root.exist?

  backups = []
  backups_root.each_child do |child|
    next unless child.directory?

    zips = child.children.select { |f| f.extname == '.zip' }.sort
    next if zips.empty? # Lightroom backups are almost always zips; skip empty/non-backup dirs

    backups << Backup.new(child, parse_backup_datetime(child), zips)
  end
  backups.sort_by! { |b| -b.dt.to_f } # newest first
  backups
end

# Group backups by a caller-supplied key and keep only the newest one per key.
#
# @param backups [Array<Backup>] backups, preferably sorted newest-first, so
#   that the first backup seen for each key is the newest
# @yield [Backup] block that computes the grouping key for a backup
# @yieldreturn [Object] the grouping key (e.g. a Date, or [year, month] pair)
# @return [Hash] mapping each key to its newest {Backup}
def latest_per_key(backups)
  latest = {}
  backups.each do |b|
    k = yield(b)
    latest[k] ||= b
  end
  latest
end

# Compute the ISO (year, week) pair for a timestamp, so that weeks are
# grouped consistently even when a week spans a year boundary.
#
# @param time [Time]
# @return [Array(Integer, Integer)] +[iso_year, iso_week]+
def iso_year_week(time)
  d = time.to_date
  [d.cwyear, d.cweek] # ISO week, stable across years
end

# Determine which backup folders should be kept under the tiered retention
# policy (daily -> weekly -> monthly, newest tier takes priority).
#
# @param backups [Array<Backup>] all discovered backups
# @param keep_days [Integer] number of most-recent daily backups to keep
# @param keep_weeks [Integer] number of weekly backups to keep among the
#   remaining older daily backups
# @param keep_months [Integer] number of monthly backups to keep among
#   whatever is left after the daily and weekly tiers
# @return [Set<Pathname>] folders to keep; anything else may be deleted
def retention_set(backups, keep_days:, keep_weeks:, keep_months:)
  # 1) Collapse multiple backups per day: keep latest of each day
  daily_latest_list = latest_per_key(backups) { |b| b.dt.to_date }.values.sort_by { |b| -b.dt.to_f }

  keep = Set.new

  # 2) Keep last N daily backups (most recent backup-days)
  daily_latest_list.first(keep_days).each { |b| keep << b.folder }

  # Remaining older daily backups
  older_daily = daily_latest_list.drop(keep_days)

  # 3) Keep last N weekly backups among older daily backups
  weekly_latest_list = latest_per_key(older_daily) { |b| iso_year_week(b.dt) }.values.sort_by { |b| -b.dt.to_f }
  weekly_latest_list.first(keep_weeks).each { |b| keep << b.folder }

  # Remove ones already kept, then do monthlies from the remainder
  older_after_weekly = older_daily.reject { |b| keep.include?(b.folder) }

  # 4) Keep last N monthly backups among what remains
  monthly_latest_list = latest_per_key(older_after_weekly) { |b| [b.dt.year, b.dt.month] }.values.sort_by { |b| -b.dt.to_f }
  monthly_latest_list.first(keep_months).each { |b| keep << b.folder }

  keep
end

# Parse command-line options.
#
# @param argv [Array<String>] arguments to parse (typically +ARGV+); an
#   optional remaining positional argument is used as the backups root,
#   otherwise a default path is used
# @return [Hash] parsed options, including +:backups_root+ [Pathname]
# @note exits the process (status 0 for +--help+)
def parse_args(argv)
  options = { keep_days: 7, keep_weeks: 4, keep_months: 3, apply: false }

  parser = OptionParser.new do |opts|
    opts.banner = 'Usage: lrc-backup-retention.rb [options] [BACKUPS_ROOT]'
    opts.separator ''
    opts.separator "BACKUPS_ROOT defaults to: #{DEFAULT_BACKUPS_ROOT}"
    opts.on('--keep-days N', Integer, 'Number of daily backups to keep (default: 7)') { |v| options[:keep_days] = v }
    opts.on('--keep-weeks N', Integer, 'Number of weekly backups to keep (default: 4)') { |v| options[:keep_weeks] = v }
    opts.on('--keep-months N', Integer, 'Number of monthly backups to keep (default: 3)') { |v| options[:keep_months] = v }
    opts.on('--apply', 'Actually delete; otherwise dry-run') { options[:apply] = true }
    opts.on('-h', '--help', 'Show this help') do
      puts opts
      exit 0
    end
  end
  parser.parse!(argv)

  options[:backups_root] = argv.empty? ? DEFAULT_BACKUPS_ROOT : Pathname.new(argv.first)
  options
end

# Entry point: parse args, scan backups, compute the retention set, report
# what will be kept/deleted, and delete when +--apply+ is passed.
#
# @return [Integer] process exit status
def main
  opts = parse_args(ARGV)
  backups = find_backups(opts[:backups_root])

  if backups.empty?
    puts "No backup folders with .zip files found under: #{opts[:backups_root]}"
    return 0
  end

  keep_folders = retention_set(
    backups,
    keep_days: opts[:keep_days],
    keep_weeks: opts[:keep_weeks],
    keep_months: opts[:keep_months]
  )

  to_delete = backups.reject { |b| keep_folders.include?(b.folder) }

  puts "Found backups: #{backups.size}"
  puts "Keeping:       #{keep_folders.size} folders"
  puts "Deleting:      #{to_delete.size} folders"
  puts

  puts 'KEEP:'
  backups.select { |b| keep_folders.include?(b.folder) }
         .sort_by { |b| -b.dt.to_f }
         .each { |b| puts "  #{b.dt.strftime('%Y-%m-%d %H:%M')}  #{b.folder.basename}" }

  puts to_delete.empty? ? "\nDELETE: (none)" : "\nDELETE:"
  to_delete.each { |b| puts "  #{b.dt.strftime('%Y-%m-%d %H:%M')}  #{b.folder.basename}" }

  unless opts[:apply]
    puts "\nDry-run only. Re-run with --apply to delete."
    return 0
  end

  puts "\nApplying deletions..."
  to_delete.each do |b|
    FileUtils.rm_rf(b.folder)
    puts "Deleted #{b.folder}"
  end

  puts 'Done.'
  0
end

exit(main) if __FILE__ == $PROGRAM_NAME
