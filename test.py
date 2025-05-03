import pstats
p = pstats.Stats('app.profile')
p.strip_dirs().sort_stats('cumulative').print_stats(10)
